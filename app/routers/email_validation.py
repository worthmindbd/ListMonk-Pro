"""Email Validation: address checks, subscriber scans, results and the
scheduled "dead email" cleanup settings."""

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.services import email_cleanup as cleanup
from app.services import email_validation as ev
from app.services.bulk_ops import parse_ids
from app.services.export_service import dict_list_to_csv
from app.services.listmonk_client import listmonk

logger = logging.getLogger("email_validation_router")

router = APIRouter()

# The UI shows at most 100 rows per page; the cap keeps one response small.
MAX_PAGE = 100_000
MAX_PER_PAGE = 200
# One quick-check request validates at most this many addresses.
MAX_CHECK_EMAILS = 100
VERDICT_FILTERS = ("invalid", "risky", "unknown")

EXPORT_COLUMNS = [
    "email", "name", "verdict", "reasons", "suggestion", "detail",
    "domain", "checked_at", "subscriber_id",
]


def _verdict_filter(verdict: str) -> str:
    verdict = (verdict or "").strip().lower()
    if verdict and verdict not in VERDICT_FILTERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid verdict: {verdict}. Use invalid, risky or unknown",
        )
    return verdict


# ── Quick check ─────────────────────────────────────────────────────────────

@router.post("/check")
async def check_emails(data: dict):
    """Validate typed or pasted addresses. Never touches ListMonk."""
    raw = data.get("emails") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not all(isinstance(e, str) for e in raw):
        raise HTTPException(status_code=400, detail="emails must be a list of strings")
    emails = list(dict.fromkeys(e.strip() for e in raw if e.strip()))
    if not emails:
        raise HTTPException(status_code=400, detail="Enter at least one email address")
    if len(emails) > MAX_CHECK_EMAILS:
        raise HTTPException(
            status_code=400,
            detail=f"Check at most {MAX_CHECK_EMAILS} addresses at a time "
                   "(use Scan subscribers for a whole list)",
        )
    return {"results": await ev.validate_many(emails)}


# ── Subscriber scan ─────────────────────────────────────────────────────────

@router.get("/status")
async def get_status():
    """Progress of the running job plus the latest scan summary."""
    state = cleanup.load_state()
    return {
        "job": cleanup.job_snapshot(),
        "last_scan": state["last_scan"],
        "counts": cleanup.result_counts(),
    }


@router.post("/scan")
async def start_scan(data: dict | None = None):
    """Start a report-only scan of enabled subscribers in the background."""
    list_id = None
    if data:
        list_id = data.get("list_id")
        if list_id is not None and (
            isinstance(list_id, bool) or not isinstance(list_id, int) or list_id < 1
        ):
            raise HTTPException(status_code=400, detail="list_id must be a positive whole number")
    try:
        cleanup.start_job(listmonk, "scan", list_id)
    except cleanup.JobBusy as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "started", "job": cleanup.job_snapshot()}


@router.post("/scan/cancel")
async def cancel_scan():
    if not cleanup.request_cancel():
        raise HTTPException(status_code=409, detail="Nothing is running")
    return {"status": "cancelling"}


# ── Results ─────────────────────────────────────────────────────────────────

@router.get("/results")
async def get_results(
    verdict: str = "",
    search: str = Query("", max_length=200),
    page: int = Query(1, ge=1, le=MAX_PAGE),
    per_page: int = Query(25, ge=1, le=MAX_PER_PAGE),
):
    verdict = _verdict_filter(verdict)
    rows = cleanup.query_results(verdict, search)
    start = (page - 1) * per_page
    return {
        "results": rows[start:start + per_page],
        "total": len(rows),
        "page": page,
        "per_page": per_page,
        "counts": cleanup.result_counts(),
    }


# Registered before any parameterised route (none today, but bulk and export
# paths must never be shadowed by a future /results/{id}).
@router.get("/results/export")
async def export_results(verdict: str = ""):
    verdict = _verdict_filter(verdict)
    rows = cleanup.query_results(verdict)
    if not rows:
        raise HTTPException(status_code=404, detail="No results to export")
    flat = [{**r, "reasons": "; ".join(r.get("reasons") or [])} for r in rows]
    return StreamingResponse(
        dict_list_to_csv(flat, EXPORT_COLUMNS),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=email_validation_results.csv"},
    )


@router.put("/results/blocklist")
async def blocklist_results(data: dict):
    """Blocklist the selected problem addresses (only the submitted ids)."""
    try:
        ids = parse_ids(data.get("ids") if isinstance(data, dict) else None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ids:
        raise HTTPException(status_code=400, detail="No subscriber ids provided")
    return await cleanup.blocklist_results(listmonk, ids)


@router.delete("/results")
async def dismiss_results(ids: list[int] = Query(...)):
    """Drop the selected rows from the results list. ListMonk is not touched."""
    try:
        parsed = parse_ids(ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not parsed:
        raise HTTPException(status_code=400, detail="No subscriber ids provided")
    return {"dismissed": await cleanup.dismiss_results(parsed)}


# ── Scheduled cleanup ───────────────────────────────────────────────────────

@router.get("/cleanup")
async def get_cleanup():
    return cleanup.view()


@router.put("/cleanup")
async def update_cleanup(data: dict):
    try:
        patch = cleanup.validate_patch(data)
        state = await cleanup.update_settings(patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        logger.error(f"Could not write email cleanup settings: {e}")
        raise HTTPException(
            status_code=500,
            detail="Could not write the cleanup settings (check data directory permissions).",
        )
    return {"status": "ok", "cleanup": cleanup.view(state)}


@router.post("/cleanup/run")
async def run_cleanup():
    """Scan everyone and blocklist the dead addresses now, in the background."""
    state = cleanup.load_state()
    if not (state["clean_invalid"] or state["clean_hard_bounces"]):
        raise HTTPException(status_code=400, detail="Choose at least one kind of address to clean")
    try:
        cleanup.start_job(listmonk, "cleanup")
    except cleanup.JobBusy as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"status": "started", "job": cleanup.job_snapshot()}
