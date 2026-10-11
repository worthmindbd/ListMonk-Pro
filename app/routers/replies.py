"""Replies router for ListMonk Pro Cold Outreach.

Provides API endpoints for prospect reply tracking, sentiment filtering,
reply feed pagination, and manual IMAP scans.
"""

import logging
from fastapi import APIRouter, HTTPException, Query, Request
from app.services.reply_log import (
    load_log, save_log, update_reply_status, load_settings, save_settings,
    _log_lock
)
from app.services.imap_reply_tracker import scan_and_track_replies
from app.services.listmonk_client import listmonk
from app.config import settings

logger = logging.getLogger("replies_router")
router = APIRouter()

MAX_PAGE = 1000
MAX_PER_PAGE = 100


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
    save_settings(data)
    return load_settings()


@router.get("/stats")
async def get_reply_stats():
    """Return aggregated statistics on replies and sentiments."""
    records = load_log()
    total = len(records)
    positive = sum(1 for r in records if r.get("sentiment") == "positive")
    question = sum(1 for r in records if r.get("sentiment") == "question")
    auto_reply = sum(1 for r in records if r.get("sentiment") == "auto_reply")
    negative = sum(1 for r in records if r.get("sentiment") == "negative")

    # Fetch total sent across campaigns to calculate accurate reply rate
    total_sent = 0
    try:
        camp_res = await listmonk.get_campaigns(page=1, per_page=100)
        camps = camp_res.get("data", {}).get("results", [])
        total_sent = sum(c.get("sent", 0) for c in camps)
    except Exception:
        pass

    reply_rate = round((total / total_sent * 100), 2) if total_sent > 0 else 0.0

    return {
        "total_replies": total,
        "positive": positive,
        "question": question,
        "auto_reply": auto_reply,
        "negative": negative,
        "total_sent": total_sent,
        "reply_rate": reply_rate,
        "positive_rate": round((positive / total_sent * 100), 2) if total_sent > 0 else 0.0,
    }


@router.get("")
async def get_replies(
    page: int = Query(1, ge=1, le=MAX_PAGE),
    per_page: int = Query(25, ge=1, le=MAX_PER_PAGE),
    sentiment: str = Query("", description="positive, question, auto_reply, negative"),
    status: str = Query("", description="unread, lead, replied, archived"),
    campaign_id: int = Query(0, description="Optional campaign ID filter"),
    q: str = Query("", description="Search term for email, name, subject, or body"),
):
    """Return paginated prospect replies with filtering."""
    records = load_log()

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
        return {"status": "ok", "stats": stats}
    except Exception as e:
        logger.error(f"Reply scan failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"IMAP scan failed: {e}")


@router.put("/{reply_id}/status")
async def update_status(reply_id: str, request: Request):
    """Update status of a prospect reply."""
    try:
        body = await request.json()
        new_status = body.get("status")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    valid_statuses = {"unread", "lead", "replied", "archived"}
    if new_status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Status must be one of {valid_statuses}"
        )

    updated = update_reply_status(reply_id, new_status)
    if not updated:
        raise HTTPException(status_code=404, detail="Reply not found")

    return {"status": "ok", "id": reply_id, "new_status": new_status}


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
