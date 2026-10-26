import asyncio
import logging
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from typing import Optional
from app.services.listmonk_client import listmonk
from app.services.bounce_ingest import ingest_bounce_mailbox
from app.services.bounce_list import iter_filtered_bounces, fetch_filtered_bounces_page
from app.services.bounce_filters import bounce_campaign_id, bounce_campaign_name
from app.services.export_service import aiter_dicts_to_csv
from app.services.bulk_ops import run_bulk, parse_ids, MAX_BULK_IDS
from app.services.hard_bounce_cache import schedule_hard_bounce_update

router = APIRouter()
logger = logging.getLogger(__name__)

_DELETE_CONCURRENCY = 10
# Delete in chunks so a full-table delete never creates thousands of
# in-flight tasks at once.
_DELETE_CHUNK = 200

# Bounce listing is the most expensive endpoint: serving page N requires
# scanning and opener-filtering every preceding record, so an unbounded
# per_page multiplies into tens of thousands of ListMonk queries. Cap both.
# The scan itself is bounded by bounce_list.MAX_LM_PAGES (30,000 raw rows),
# so MAX_PAGE only has to reach that far: at 100 the UI's 25-per-page pager
# 422'd on every page past 100 for any installation over 2,500 bounces.
MAX_PAGE = 1500
MAX_PER_PAGE = 100

# ListMonk filters `type` with a Postgres enum cast, so an unknown value makes
# its query fail (500). Validate before forwarding anything upstream.
VALID_BOUNCE_TYPES = {"", "all", "hard", "soft", "complaint"}


def _validate_bounce_type(bounce_type: str) -> str:
    normalized = (bounce_type or "").strip().lower()
    if normalized not in VALID_BOUNCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid bounce_type: {bounce_type}. Use hard, soft, complaint, or all",
        )
    return normalized


@router.post("/ingest")
async def ingest_bounces():
    """Scan the bounce IMAP mailbox for new bounces, classify each, and
    create matching bounce records in ListMonk."""
    try:
        result = await ingest_bounce_mailbox(listmonk)
        schedule_hard_bounce_update()
        return result
    except Exception as e:
        logger.error(f"bounce ingest failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
async def get_bounces(page: int = Query(1, ge=1, le=MAX_PAGE),
                      per_page: int = Query(50, ge=1, le=MAX_PER_PAGE),
                      campaign_id: Optional[int] = None, source: str = "",
                      bounce_type: str = ""):
    return await fetch_filtered_bounces_page(
        listmonk, page, per_page, _validate_bounce_type(bounce_type), campaign_id, source,
    )


@router.get("/export")
async def export_bounces(campaign_id: Optional[int] = None, source: str = "",
                         bounce_type: str = ""):
    """Export all bounce records (optionally filtered) as CSV.

    Rows stream one ListMonk page at a time, so the export never buffers the
    whole (potentially huge) bounce table in memory.
    """
    bounce_type = _validate_bounce_type(bounce_type)

    async def rows():
        async for b in iter_filtered_bounces(
            listmonk, bounce_type=bounce_type, campaign_id=campaign_id, source=source,
        ):
            b["campaign_name"] = bounce_campaign_name(b)
            b["campaign_id"] = bounce_campaign_id(b) or ""
            yield b

    # Peek one row so an empty result is a 404 rather than a 0-byte file.
    stream = rows()
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        raise HTTPException(status_code=404, detail="No bounce records found")

    async def with_first():
        yield first
        async for b in stream:
            yield b

    columns = ["id", "email", "campaign_id", "campaign_name", "type", "source", "created_at"]
    suffix = ""
    if campaign_id:
        suffix += f"_campaign_{campaign_id}"
    if bounce_type:
        suffix += f"_{bounce_type}"
    return StreamingResponse(
        aiter_dicts_to_csv(with_first(), columns),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=bounces{suffix}.csv"},
    )


@router.delete("/{bounce_id}")
async def delete_bounce(bounce_id: int):
    res = await listmonk.delete_bounce(bounce_id)
    schedule_hard_bounce_update()
    return res


@router.delete("")
async def delete_all_bounces(campaign_id: Optional[int] = None,
                              bounce_type: str = "",
                              ids: list[int] = Query(default=[]),
                              confirm: bool = Query(False)):
    """Delete bounces. If campaign_id is provided, only delete bounces for
    that campaign (iterating + deleting in parallel). If bounce_type is set
    without campaign_id, delete all matching bounces. Otherwise delete all.

    Supplying ``ids`` narrows the action to an explicit selection, which is
    what the Bounces page's bulk bar uses — every other mode is a filter-wide
    sweep. The unfiltered delete-all (no ids, no campaign_id, no bounce_type)
    requires ``confirm=true`` to prevent accidental data loss.
    """
    bounce_type = _validate_bounce_type(bounce_type)

    if ids:
        # Shared selection validation (rule 14): dedupe + reject junk/<1 ids
        # so a repeated row cannot 404 twice and inflate the error count.
        try:
            parsed_ids = parse_ids(ids)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not parsed_ids:
            raise HTTPException(status_code=400, detail="No bounce ids provided")
        result = await run_bulk(parsed_ids, listmonk.delete_bounce)
        schedule_hard_bounce_update()
        return {
            "deleted": result["ok"],
            "errors": result["errors"],
            "failed": result["failed"],
        }

    if not campaign_id and not bounce_type:
        if not confirm:
            raise HTTPException(
                status_code=400,
                detail="Pass confirm=true to delete ALL bounces. This cannot be undone.",
            )
        logger.warning("Deleting ALL bounces (confirm=true)")
        # ListMonk's delete-all answers a bare {"data": true}; count first so
        # the response has the same {deleted, errors, failed} shape as every
        # other branch (the UI reported "Deleted: 0" after wiping the table).
        try:
            before = await listmonk.get_bounces(1, 1)
            total = int((before.get("data") or {}).get("total") or 0)
        except Exception as e:
            logger.warning(f"Could not count bounces before delete-all: {e}")
            total = None
        res = await listmonk.delete_all_bounces()
        schedule_hard_bounce_update()
        return {**res, "deleted": total, "errors": 0, "failed": [], "all": True}

    # Collect only the IDs (a few bytes each) rather than materialising every
    # bounce record, which can be millions of rows with large meta payloads.
    bounce_ids: list[int] = []
    async for b in iter_filtered_bounces(listmonk, bounce_type, campaign_id):
        bid = b.get("id")
        if bid:
            bounce_ids.append(bid)

    if not bounce_ids:
        return {"deleted": 0, "errors": 0, "failed": [], "campaign_id": campaign_id}

    sem = asyncio.Semaphore(_DELETE_CONCURRENCY)
    deleted = 0
    errors = 0
    failed: list[dict] = []

    async def _delete(bid: int):
        nonlocal deleted, errors
        async with sem:
            try:
                await listmonk.delete_bounce(bid)
                deleted += 1
            except Exception as exc:
                errors += 1
                failed.append({"id": bid, "error": str(exc)})
                logger.error(f"Failed to delete bounce {bid}: {exc}")

    for start in range(0, len(bounce_ids), _DELETE_CHUNK):
        chunk = bounce_ids[start:start + _DELETE_CHUNK]
        await asyncio.gather(*(_delete(bid) for bid in chunk))

    schedule_hard_bounce_update()
    # Same partial-failure shape as the ids branch: the caller can see WHICH
    # records failed, not just how many.
    return {"deleted": deleted, "errors": errors, "failed": failed,
            "campaign_id": campaign_id}
