"""Replies router for ListMonk Pro Cold Outreach.

Provides API endpoints for prospect reply tracking, sentiment filtering,
reply feed pagination, and manual IMAP scans.
"""

import logging
from fastapi import APIRouter, HTTPException, Query, Request
from app.services.reply_log import (
    load_log, save_log, update_reply_status, update_reply_statuses, delete_replies,
    load_settings, save_settings, _log_lock,
)
from app.services.bulk_ops import run_bulk
from app.services.imap_reply_tracker import scan_and_track_replies
from app.services.imap_helpers import safe_email_for_query
from app.services.listmonk_client import listmonk
from app.config import settings

logger = logging.getLogger("replies_router")
router = APIRouter()

MAX_PAGE = 1000
MAX_PER_PAGE = 100

# Bulk actions fan out to ListMonk (one lookup + one blocklist per sender), so
# the selection is capped like every other bulk endpoint.
MAX_BULK_IDS = 500

VALID_REPLY_STATUSES = {"unread", "lead", "replied", "archived"}


def _bulk_reply_ids(raw) -> list[str]:
    """Validate the ``ids`` payload of a bulk reply action."""
    if not isinstance(raw, (list, tuple, set)):
        raise ValueError("ids must be a list of reply ids")
    if len(raw) > MAX_BULK_IDS:
        raise ValueError(f"Too many ids (max {MAX_BULK_IDS})")
    return [str(i) for i in raw if str(i or "").strip()]


def _records_for(ids: list[str]) -> list[dict]:
    wanted = {str(i) for i in ids}
    return [r for r in load_log() if str(r.get("id")) in wanted]


@router.get("/status")
async def get_reply_status():
    """Check IMAP configuration status."""
    return {
        "configured": settings.imap_configured,
        "host": settings.imap_host,
        "user": settings.imap_user,
        "port": settings.imap_port,
        "ssl": settings.imap_use_ssl,
    }


@router.get("/settings")
async def get_reply_settings():
    return load_settings()


@router.put("/settings")
async def update_reply_settings(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Settings must be a JSON object")
    # Only merge keys this tracker actually owns: an arbitrary client dict
    # (including multi-megabyte junk) must not be persisted to DATA_DIR.
    allowed = set(load_settings().keys())
    save_settings({k: v for k, v in data.items() if k in allowed})
    return load_settings()


async def _reply_rate_sent_total(campaign_id: int = 0) -> int:
    """Sent total used as the reply-rate denominator.

    For the whole account the cached campaign summary is reused, because the
    newest campaign page alone would make the reply rate look inflated. A
    single campaign is scoped to its own ``sent`` count.
    """
    if campaign_id:
        try:
            result = await listmonk.get_campaign(campaign_id)
            return int((result.get("data") or {}).get("sent") or 0)
        except Exception as e:
            logger.warning(f"Could not compute sent total for campaign {campaign_id}: {e}")
            return 0

    try:
        from app.routers.campaigns import get_campaigns_summary

        summary = await get_campaigns_summary()
        return int(summary.get("sent", 0) or 0)
    except Exception as e:
        logger.warning(f"Could not compute total sent for reply rate: {e}")
        return 0


@router.get("/stats")
async def get_reply_stats(campaign_id: int = Query(0, description="Optional campaign ID filter")):
    """Return aggregated statistics on replies and sentiments.

    With ``campaign_id`` the counts and the reply rate are scoped to that
    campaign, so the Analytics page can show per-campaign reply performance
    without re-deriving the denominator in the browser.
    """
    # Delivery-failure DSNs are never stored as replies anymore (they are
    # ingested as bounces); the sentiment filter is a defensive guard for
    # logs written by older versions, so a leftover DSN can never surface.
    all_records = [r for r in load_log() if r.get("sentiment") != "bounce"]
    records = all_records
    if campaign_id:
        records = [r for r in all_records
                   if str(r.get("campaign_id")) == str(campaign_id)]

    total = len(records)
    positive = sum(1 for r in records if r.get("sentiment") == "positive")
    auto_reply = sum(1 for r in records if r.get("sentiment") == "auto_reply")
    negative = sum(1 for r in records if r.get("sentiment") == "negative")

    # Reply counts per campaign (always from the full log, even for a scoped
    # call) power the Analytics campaign comparison chart.
    by_campaign: dict[str, int] = {}
    for r in all_records:
        cid = r.get("campaign_id")
        if cid:
            key = str(cid)
            by_campaign[key] = by_campaign.get(key, 0) + 1

    total_sent = await _reply_rate_sent_total(campaign_id)
    reply_rate = round((total / total_sent * 100), 2) if total_sent > 0 else 0.0

    return {
        "campaign_id": campaign_id,
        "total_replies": total,
        "positive": positive,
        "auto_reply": auto_reply,
        "negative": negative,
        "total_sent": total_sent,
        "reply_rate": reply_rate,
        "positive_rate": round((positive / total_sent * 100), 2) if total_sent > 0 else 0.0,
        "by_campaign": by_campaign,
    }


@router.get("")
async def get_replies(
    page: int = Query(1, ge=1, le=MAX_PAGE),
    per_page: int = Query(25, ge=1, le=MAX_PER_PAGE),
    sentiment: str = Query("", description="positive, auto_reply, negative"),
    status: str = Query("", description="unread, lead, replied, archived"),
    campaign_id: int = Query(0, description="Optional campaign ID filter"),
    q: str = Query("", description="Search term for email, name, subject, or body"),
):
    """Return paginated prospect replies with filtering."""
    # Delivery-failure DSNs are ingested as bounces, never shown here; the
    # sentiment filter drops leftovers from logs written by older versions.
    records = [r for r in load_log() if r.get("sentiment") != "bounce"]

    # Filter
    filtered = records
    if sentiment:
        filtered = [r for r in filtered if r.get("sentiment") == sentiment]
    if status:
        filtered = [r for r in filtered if r.get("status") == status]
    if campaign_id:
        filtered = [r for r in filtered if r.get("campaign_id") == campaign_id]
    if q:
        query_lower = q.lower().strip()
        filtered = [
            r for r in filtered
            if query_lower in r.get("from_email", "").lower()
            or query_lower in r.get("from_name", "").lower()
            or query_lower in r.get("subject", "").lower()
            or query_lower in r.get("full_body", "").lower()
        ]

    total = len(filtered)
    start = (page - 1) * per_page
    end = start + per_page
    results = filtered[start:end]

    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "results": results,
    }


@router.post("/scan")
async def scan_replies():
    """Trigger an immediate IMAP scan for prospect replies."""
    try:
        stats = await scan_and_track_replies(listmonk)
    except Exception as e:
        logger.error(f"Reply scan failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"IMAP scan failed: {e}")
    # The service catches its own IMAP errors and returns them in the body;
    # rule 7 says a failed scan must not answer 200 "ok" — the UI used to
    # toast "Scan complete! Found 0 replies" during a full IMAP outage.
    # ("already in progress" carries error=None and stays a 200.)
    if stats.get("error"):
        raise HTTPException(status_code=502, detail=f"IMAP scan failed: {stats['error']}")
    return {"status": "ok", "stats": stats}


@router.put("/status")
async def update_statuses(request: Request):
    """Update the status of several replies in one request.

    The reply feed's bulk bar posts here: marking a page of replies as
    "replied" or "archived" should not cost one request per card.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    new_status = body.get("status")
    if new_status not in VALID_REPLY_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Status must be one of {sorted(VALID_REPLY_STATUSES)}"
        )
    try:
        ids = _bulk_reply_ids(body.get("ids"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not ids:
        raise HTTPException(status_code=400, detail="No reply ids provided")

    async with _log_lock:
        updated = update_reply_statuses(ids, new_status)

    if not updated:
        raise HTTPException(status_code=404, detail="No matching replies found")
    return {"status": "ok", "updated": updated, "new_status": new_status}


@router.post("/blocklist")
async def blocklist_reply_senders(request: Request):
    """Blocklist the ListMonk subscriber behind each selected reply."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    try:
        ids = _bulk_reply_ids(body.get("ids"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    records = _records_for(ids)
    if not records:
        raise HTTPException(status_code=404, detail="No matching replies found")

    emails = {r["from_email"] for r in records if (r.get("from_email") or "").strip()}
    if not emails:
        raise HTTPException(status_code=400, detail="Selected replies have no sender email")

    async def _blocklist(email: str) -> None:
        await _blocklist_sender(email)

    result = await run_bulk(sorted(emails), _blocklist)
    return {
        "status": "ok",
        "blocklisted": result["ok"],
        "errors": result["errors"],
        "failed": result["failed"],
    }


@router.put("/{reply_id}/status")
async def update_status(reply_id: str, request: Request):
    """Update status of a prospect reply."""
    try:
        body = await request.json()
        new_status = body.get("status")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if new_status not in VALID_REPLY_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Status must be one of {VALID_REPLY_STATUSES}"
        )

    # Same lock as every other log mutation: an unlocked load→mutate→save
    # could be interleaved with the scanner's append (or a bulk update) and
    # silently drop whichever write ran last.
    async with _log_lock:
        updated = update_reply_status(reply_id, new_status)
    if not updated:
        raise HTTPException(status_code=404, detail="Reply not found")

    return {"status": "ok", "id": reply_id, "new_status": new_status}


async def _blocklist_sender(email: str) -> dict:
    """Blocklist the ListMonk subscriber behind a sender address.

    Resolving the email server-side keeps the address out of the client-built
    ListMonk query string and validates it with the same helper the scanners
    use, so a crafted Reply-To address cannot inject query syntax.
    """
    safe_email = safe_email_for_query((email or "").strip())
    if not safe_email:
        raise HTTPException(status_code=400, detail="Reply has no valid sender email")

    try:
        result = await listmonk.get_subscribers(1, 1, f"subscribers.email = '{safe_email}'")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Subscriber lookup failed for {email}: {e}")
        raise HTTPException(status_code=502, detail=f"Subscriber lookup failed: {e}")
    subscribers = (result.get("data") or {}).get("results") or []
    if not subscribers:
        raise HTTPException(status_code=404, detail=f"No ListMonk subscriber found for {email}")

    subscriber_id = subscribers[0]["id"]
    try:
        await listmonk.blocklist_subscriber(subscriber_id)
    except Exception as e:
        logger.error(f"Blocklist failed for {email}: {e}")
        raise HTTPException(status_code=502, detail=f"Blocklist failed: {e}")

    return {"status": "ok", "subscriber_id": subscriber_id, "email": email}


@router.post("/{reply_id}/blocklist")
async def blocklist_reply_sender(reply_id: str):
    """Blocklist the ListMonk subscriber behind a prospect reply."""
    records = load_log()
    record = next((r for r in records if str(r.get("id")) == str(reply_id)), None)
    if not record:
        raise HTTPException(status_code=404, detail="Reply not found")

    return await _blocklist_sender(record.get("from_email") or "")


@router.delete("")
async def delete_replies_bulk(ids: list[str] = Query(default=[])):
    """Delete several replies from the log in one request."""
    try:
        reply_ids = _bulk_reply_ids(ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not reply_ids:
        raise HTTPException(status_code=400, detail="No reply ids provided")

    async with _log_lock:
        removed = delete_replies(reply_ids)

    if not removed:
        raise HTTPException(status_code=404, detail="No matching replies found")
    return {"status": "ok", "deleted": removed}


@router.delete("/{reply_id}")
async def delete_reply(reply_id: str):
    """Delete a reply from the log."""
    async with _log_lock:
        records = load_log()
        new_records = [r for r in records if str(r.get("id")) != str(reply_id)]
        if len(new_records) == len(records):
            raise HTTPException(status_code=404, detail="Reply not found")
        save_log(new_records)
    return {"status": "ok", "deleted": reply_id}
