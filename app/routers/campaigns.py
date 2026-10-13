import json
import re
import time
from datetime import date, timedelta
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from typing import Optional
from app.services.listmonk_client import listmonk
from app.services.bulk_ops import parse_ids, run_bulk
from app.services.export_service import dict_list_to_csv, aiter_dicts_to_csv
from app.services.bounce_list import fetch_filtered_bounces_page, iter_filtered_bounces
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

# The analytics type is interpolated into the upstream ListMonk URL, so it
# must never be an arbitrary (e.g. %2F-encoded path-traversal) segment.
VALID_ANALYTICS_TYPES = {"views", "clicks", "bounces", "links"}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Short-lived cache for the all-campaigns summary (paginates the entire
# campaign history, so avoid repeating it on every dashboard/analytics load).
_SUMMARY_TTL = 60
_summary_cache: dict = {}
_summary_cached_at = 0.0


def invalidate_summary_cache() -> None:
    """Drop the cached summary so the next request re-aggregates.

    Module-level caches need an invalidation hook (AGENTS.md rule 11);
    tests/conftest.py calls this between tests.
    """
    global _summary_cache, _summary_cached_at
    _summary_cache = {}
    _summary_cached_at = 0.0


def _validate_date(value: str, name: str) -> str:
    """Validate an optional YYYY-MM-DD filter bound (400 on anything else).

    The value is interpolated into a ListMonk SQL query, so the strict format
    check is what makes that safe.
    """
    if not value:
        return ""
    if not _DATE_RE.match(value):
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} is not a valid date")
    return value


def _hard_counts_ready() -> bool:
    """True once the hard-bounce cache has completed at least one refresh."""
    return bool(get_last_updated())


def _engagement_query(campaign_id: int, engagement_type: str,
                      from_date: str = "", to_date: str = "") -> str | None:
    """SQL clause matching subscribers with a view/click row for the campaign.

    Optional YYYY-MM-DD bounds (already validated) narrow the subquery to the
    same window the analytics charts show, so "Export Who Opened" next to a
    date-filtered chart exports exactly what the chart counts.
    """
    table = {"views": "campaign_views", "clicks": "link_clicks"}.get(engagement_type)
    if not table:
        return None
    bounds = ""
    if from_date:
        bounds += f" AND created_at >= '{from_date}'"
    if to_date:
        # Inclusive end date: bounded by the following midnight.
        next_day = (date.fromisoformat(to_date) + timedelta(days=1)).isoformat()
        bounds += f" AND created_at < '{next_day}'"
    return (
        f"subscribers.id IN (SELECT subscriber_id FROM {table} "
        f"WHERE campaign_id={campaign_id}{bounds})"
    )


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
    if analytics_type not in VALID_ANALYTICS_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid analytics type: {analytics_type}. Use views, clicks, bounces, or links",
        )
    return await listmonk.get_campaign_analytics(analytics_type, campaign_id,
                                                 from_date, to_date)


@router.get("/analytics/{analytics_type}/export")
async def export_campaign_analytics(analytics_type: str,
                                    campaign_id: int = 0,
                                    from_date: str = "", to_date: str = ""):
    """Export campaign analytics as CSV."""
    if analytics_type not in VALID_ANALYTICS_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid analytics type: {analytics_type}. Use views, clicks, bounces, or links",
        )
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


# Registered AFTER the two real analytics routes, so single-segment types and
# .../export still match above. A %2F-encoded traversal value (e.g.
# "..%2F..%2Fsettings") is percent-decoded into extra path segments before
# routing, which the single-segment routes can never match — without this
# catch-all it would be an anonymous 404 instead of an explicit rejection.
@router.get("/analytics/{traversal:path}")
async def reject_analytics_path_traversal(traversal: str):
    raise HTTPException(
        status_code=400,
        detail=f"Invalid analytics type: {traversal}. Use views, clicks, bounces, or links",
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
async def export_campaign_subscribers(campaign_id: int, engagement_type: str,
                                      from_date: str = "", to_date: str = ""):
    """Export subscribers who viewed/clicked/bounced a campaign as CSV.

    Every branch streams one page at a time, so a campaign with a huge
    audience does not have to be buffered in memory to be exported.
    Optional YYYY-MM-DD bounds narrow the export to the same window the
    analytics charts are showing.
    """
    from_date = _validate_date(from_date, "from_date")
    to_date = _validate_date(to_date, "to_date")

    def _in_range(created_at) -> bool:
        if not from_date and not to_date:
            return True
        day = str(created_at or "")[:10]
        if not day:
            return False
        if from_date and day < from_date:
            return False
        if to_date and day > to_date:
            return False
        return True

    if engagement_type == "bounces":
        async def bounce_rows():
            async for b in iter_filtered_bounces(listmonk, "hard", campaign_id):
                if _in_range(b.get("created_at")):
                    yield b

        stream = bounce_rows()
        try:
            first = await anext(stream)
        except StopAsyncIteration:
            raise HTTPException(status_code=404, detail="No hard bounce records found")

        async def with_first_bounce():
            yield first
            async for b in stream:
                yield b

        columns = ["email", "type", "source", "created_at"]
        return StreamingResponse(
            aiter_dicts_to_csv(with_first_bounce(), columns),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=campaign_{campaign_id}_hard_bounces.csv"},
        )

    query = _engagement_query(campaign_id, engagement_type, from_date, to_date)
    if not query:
        raise HTTPException(status_code=400, detail=f"Invalid type: {engagement_type}. Use views, clicks, or bounces")

    async def subscriber_rows():
        async for sub in listmonk.iter_pages(
            listmonk.get_subscribers, per_page=100, query=query,
        ):
            sub["lists"] = ", ".join(l.get("name", "") for l in (sub.get("lists") or []))
            attribs = sub.get("attribs", {})
            if isinstance(attribs, dict):
                sub["attribs"] = json.dumps(attribs, ensure_ascii=False) if attribs else ""
            yield sub

    # Peek one row so an empty result is still a 404 rather than a 0-byte file.
    stream = subscriber_rows()
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        raise HTTPException(status_code=404, detail=f"No {engagement_type} subscribers found")

    async def with_first_subscriber():
        yield first
        async for sub in stream:
            yield sub

    columns = ["id", "email", "name", "status", "lists", "attribs", "created_at"]
    return StreamingResponse(
        aiter_dicts_to_csv(with_first_subscriber(), columns),
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


# NOTE: the static /archive route must be registered before the parameterized
# /{campaign_id} routes below, otherwise Starlette int-parses "archive" (422).
@router.put("/archive")
async def archive_campaigns(data: dict):
    """Archive several campaigns in one request.

    ListMonk only archives one campaign per call, so the loop runs here with
    the shared bounded-concurrency helper.
    """
    try:
        ids = parse_ids(data.get("ids"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ids:
        raise HTTPException(status_code=400, detail="No campaign ids provided")

    result = await run_bulk(ids, listmonk.archive_campaign)
    return {
        "archived": result["ok"],
        "errors": result["errors"],
        "failed": result["failed"],
    }


@router.delete("")
async def delete_campaigns(ids: list[int] = Query(...)):
    """Delete several campaigns in one request."""
    try:
        parsed = parse_ids(ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not parsed:
        raise HTTPException(status_code=400, detail="No campaign ids provided")
    return await listmonk.delete_campaigns(parsed)


@router.post("/{campaign_id}/test")
async def test_campaign(campaign_id: int, data: dict):
    """Send a test copy of the campaign to the given address.

    ListMonk resolves test recipients as existing subscribers by e-mail, so
    an address that is not a subscriber is rejected upstream.
    """
    emails = data.get("emails") or data.get("subscribers") or data.get("subscriber_emails") or []
    if isinstance(emails, str):
        emails = [emails]
    emails = [e.strip().lower() for e in emails if isinstance(e, str) and e.strip()]
    if not emails:
        raise HTTPException(status_code=400, detail="Enter at least one recipient e-mail address.")
    return await listmonk.test_campaign(campaign_id, emails)


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
