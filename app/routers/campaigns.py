import json
import time
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from typing import Optional
from app.services.listmonk_client import listmonk
from app.services.export_service import dict_list_to_csv, aiter_dicts_to_csv
from app.services.bounce_filters import campaign_views_query
from app.services.bounce_list import fetch_all_filtered_bounces, fetch_filtered_bounces_page
from app.services.hard_bounce_cache import (
    get_all_hard_bounce_counts, get_last_updated,
)

router = APIRouter()

# Pagination bounds. The UI never requests more than 500, and an unbounded
# per_page turns a single request into a full-table scan in ListMonk.
MAX_PAGE = 100
MAX_PER_PAGE = 500

# Export streams pages lazily; cap the walk so a misbehaving API cannot hold
# the connection open indefinitely. 100 pages x 100 rows = 10k campaigns.
EXPORT_MAX_PAGES = 100

# Short-lived cache for the all-campaigns summary (paginates the entire
# campaign history, so avoid repeating it on every dashboard/analytics load).
_SUMMARY_TTL = 60
_summary_cache: dict = {}
_summary_cached_at = 0.0


def _hard_counts_ready() -> bool:
    """True once the hard-bounce cache has completed at least one refresh."""
    return bool(get_last_updated())


def _engagement_query(campaign_id: int, engagement_type: str) -> str | None:
    return {
        "views": campaign_views_query(campaign_id),
        "clicks": f"subscribers.id IN (SELECT subscriber_id FROM link_clicks WHERE campaign_id={campaign_id})",
    }.get(engagement_type)


@router.get("")
async def get_campaigns(page: int = Query(1, ge=1, le=MAX_PAGE),
                        per_page: int = Query(50, ge=1, le=MAX_PER_PAGE),
                        query: str = "", status: str = "",
                        order_by: str = "created_at", order: str = "DESC"):
    result = await listmonk.get_campaigns(page, per_page, query, status,
                                           order_by, order)

    # Replace bounce counts with hard bounce counts from cache. Only do this
    # once the cache has been populated — otherwise raw (soft-inclusive)
    # counts would be mixed with hard counts.
    campaigns = result.get("data", {}).get("results", [])
    if campaigns and _hard_counts_ready():
        hard_counts = get_all_hard_bounce_counts()
        for c in campaigns:
            cid = c.get("id")
            c["bounces"] = hard_counts.get(cid, 0)

    return result


@router.get("/running/stats")
async def get_running_stats(campaign_id: Optional[int] = None):
    return await listmonk.get_running_stats(campaign_id)


@router.get("/analytics/{analytics_type}")
async def get_campaign_analytics(analytics_type: str,
                                 campaign_id: int = 0,
                                 from_date: str = "", to_date: str = ""):
    return await listmonk.get_campaign_analytics(analytics_type, campaign_id,
                                                 from_date, to_date)


@router.get("/analytics/{analytics_type}/export")
async def export_campaign_analytics(analytics_type: str,
                                    campaign_id: int = 0,
                                    from_date: str = "", to_date: str = ""):
    """Export campaign analytics as CSV."""
    result = await listmonk.get_campaign_analytics(analytics_type, campaign_id,
                                                   from_date, to_date)
    data = result.get("data", [])
    if not data:
        raise HTTPException(status_code=404, detail="No analytics data found")

    columns = list(data[0].keys())
    return StreamingResponse(
        dict_list_to_csv(data, columns),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=analytics_{analytics_type}.csv"},
    )


@router.get("/export-all")
async def export_all_campaigns():
    """Stream the full campaign history as CSV, page by page."""
    columns = ["id", "name", "subject", "status", "type", "to_send", "sent",
               "views", "clicks", "bounces", "created_at", "started_at"]

    hard_counts_ready = _hard_counts_ready()
    hard_counts = get_all_hard_bounce_counts() if hard_counts_ready else {}

    async def rows():
        async for c in listmonk.iter_pages(
            listmonk.get_campaigns, per_page=100, max_pages=EXPORT_MAX_PAGES,
        ):
            if hard_counts_ready:
                c["bounces"] = hard_counts.get(c.get("id"), 0)
            yield c

    stream = rows()
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        raise HTTPException(status_code=404, detail="No campaigns found")

    async def with_first():
        yield first
        async for c in stream:
            yield c

    return StreamingResponse(
        aiter_dicts_to_csv(with_first(), columns),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=campaigns_export.csv"},
    )


@router.get("/summary")
async def get_campaigns_summary():
    """Aggregate totals across ALL campaigns for dashboard/analytics cards.

    Paginates the full campaign list, so the result is cached briefly.
    """
    global _summary_cache, _summary_cached_at
    now = time.monotonic()
    if _summary_cache and now - _summary_cached_at < _SUMMARY_TTL:
        return _summary_cache

    campaigns = await listmonk.paginate_all(listmonk.get_campaigns, per_page=100)

    status_counts: dict[str, int] = {}
    sent = views = clicks = 0
    raw_bounces = 0
    for c in campaigns:
        status = c.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        sent += c.get("sent") or 0
        views += c.get("views") or 0
        clicks += c.get("clicks") or 0
        raw_bounces += c.get("bounces") or 0

    if _hard_counts_ready():
        bounces = sum(get_all_hard_bounce_counts().values())
    else:
        bounces = raw_bounces

    _summary_cache = {
        "total_campaigns": len(campaigns),
        "status_counts": status_counts,
        "sent": sent,
        "views": views,
        "clicks": clicks,
        "bounces": bounces,
    }
    _summary_cached_at = now
    return _summary_cache


@router.get("/{campaign_id}/subscribers/{engagement_type}")
async def get_campaign_subscribers(campaign_id: int, engagement_type: str,
                                   page: int = Query(1, ge=1, le=MAX_PAGE),
                                   per_page: int = Query(50, ge=1, le=MAX_PER_PAGE)):
    """Get subscribers who viewed/clicked/bounced for a campaign."""
    if engagement_type == "bounces":
        return await fetch_filtered_bounces_page(
            listmonk, page, per_page, "hard", campaign_id,
        )

    query = _engagement_query(campaign_id, engagement_type)
    if not query:
        raise HTTPException(status_code=400, detail=f"Invalid type: {engagement_type}. Use views, clicks, or bounces")

    return await listmonk.get_subscribers(page, per_page, query)


@router.get("/{campaign_id}/subscribers/{engagement_type}/export")
async def export_campaign_subscribers(campaign_id: int, engagement_type: str):
    """Export subscribers who viewed/clicked/bounced a campaign as CSV."""
    if engagement_type == "bounces":
        hard_records = await fetch_all_filtered_bounces(
            listmonk, "hard", campaign_id,
        )
        if not hard_records:
            raise HTTPException(status_code=404, detail="No hard bounce records found")

        columns = ["email", "type", "source", "created_at"]
        return StreamingResponse(
            dict_list_to_csv(hard_records, columns),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=campaign_{campaign_id}_hard_bounces.csv"},
        )

    query = _engagement_query(campaign_id, engagement_type)
    if not query:
        raise HTTPException(status_code=400, detail=f"Invalid type: {engagement_type}. Use views, clicks, or bounces")

    all_subscribers = await listmonk.paginate_all(
        listmonk.get_subscribers, per_page=500, query=query,
    )
    if not all_subscribers:
        raise HTTPException(status_code=404, detail=f"No {engagement_type} subscribers found")

    for sub in all_subscribers:
        sub["lists"] = ", ".join(l.get("name", "") for l in sub.get("lists", []))
        attribs = sub.get("attribs", {})
        if isinstance(attribs, dict):
            sub["attribs"] = json.dumps(attribs, ensure_ascii=False) if attribs else ""

    columns = ["id", "email", "name", "status", "lists", "attribs", "created_at"]
    return StreamingResponse(
        dict_list_to_csv(all_subscribers, columns),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=campaign_{campaign_id}_{engagement_type}.csv"},
    )


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: int):
    result = await listmonk.get_campaign(campaign_id)

    # Replace bounce count with hard-bounce cache (populated at startup
    # and refreshed every 5 min). Keeps ListMonk's raw count until the
    # initial cache update completes.
    campaign = result.get("data", {})
    if campaign and _hard_counts_ready():
        cached = get_all_hard_bounce_counts()
        campaign["bounces"] = cached.get(campaign_id, 0)

    return result


@router.get("/{campaign_id}/preview")
async def preview_campaign(campaign_id: int):
    resp = await listmonk.preview_campaign(campaign_id)
    # The preview is untrusted HTML. Sandboxing (and nosniff) prevents it
    # from executing scripts if opened directly in a browser tab.
    return HTMLResponse(
        content=resp.text,
        headers={
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("")
async def create_campaign(data: dict):
    return await listmonk.create_campaign(data)


@router.post("/{campaign_id}/test")
async def test_campaign(campaign_id: int, data: dict):
    return await listmonk.test_campaign(campaign_id, data)


@router.put("/{campaign_id}")
async def update_campaign(campaign_id: int, data: dict):
    return await listmonk.update_campaign(campaign_id, data)


@router.put("/{campaign_id}/status")
async def change_campaign_status(campaign_id: int, data: dict):
    return await listmonk.change_campaign_status(campaign_id, data.get("status", ""))


@router.put("/{campaign_id}/archive")
async def archive_campaign(campaign_id: int):
    return await listmonk.archive_campaign(campaign_id)


@router.delete("/{campaign_id}")
async def delete_campaign(campaign_id: int):
    return await listmonk.delete_campaign(campaign_id)
