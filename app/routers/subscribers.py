from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse
from typing import Optional
from app.services.listmonk_client import listmonk
from app.services.export_service import aiter_dicts_to_csv
from app.services.bulk_ops import parse_ids
from app.services.search_query import build_ilike_search

router = APIRouter()

# Pagination bounds (the UI requests at most 50).
MAX_PAGE = 100
MAX_PER_PAGE = 200

# Export streams pages lazily: iter_pages terminates on an empty page or once
# ListMonk's reported total is covered, so the walk cannot run away — and an
# explicit page cap here would silently truncate the CSV mid-stream.

# ListMonk has no `status` filter parameter on /api/subscribers, but its query
# language does — this is the same clause auto_unblock uses.
VALID_STATUSES = {"enabled", "blocklisted"}


def _with_status_filter(query: str, status: str) -> str:
    """Fold a status shortcut the UI offers into a ListMonk query clause."""
    status = (status or "").strip().lower()
    if not status:
        return query
    if status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status: {status}. Use enabled or blocklisted",
        )
    clause = f"subscribers.status = '{status}'"
    return f"({query}) AND {clause}" if query else clause


def _search_to_query(search: str) -> str:
    """Turn free text from the search box into a safe ListMonk query clause.

    ListMonk's subscriber `query` parameter is a raw SQL expression, so the
    UI's plain search terms ("info@acme") used to reach Postgres unparsed and
    400 with `column "info" does not exist`. The term is wrapped in an ILIKE
    match on email/name instead: single quotes are doubled, and LIKE
    wildcards plus backslashes are stripped so the value can only ever be a
    literal substring.
    """
    return build_ilike_search(search, ("subscribers.email", "subscribers.name"))


def _compose_query(query: str, search: str, status: str) -> str:
    """Combine the raw-SQL passthrough, the free-text search and the status
    shortcut into one ListMonk query expression."""
    clauses = [c for c in (query.strip(), _search_to_query(search)) if c]
    combined = " AND ".join(f"({c})" for c in clauses) if len(clauses) > 1 \
        else (clauses[0] if clauses else "")
    return _with_status_filter(combined, status)


@router.get("")
async def get_subscribers(page: int = Query(1, ge=1, le=MAX_PAGE),
                          per_page: int = Query(50, ge=1, le=MAX_PER_PAGE),
                          query: str = "", search: str = "",
                          list_id: Optional[int] = None,
                          status: str = ""):
    return await listmonk.get_subscribers(
        page, per_page, _compose_query(query, search, status), list_id,
    )


@router.get("/export-all")
async def export_all_subscribers(query: str = "", search: str = "",
                                 list_id: Optional[int] = None,
                                 status: str = ""):
    """Stream every subscriber as CSV, page by page.

    Rows are pulled from ListMonk lazily so peak memory stays at one page
    regardless of list size. 404 when the filter matches nothing.
    """
    columns = ["id", "email", "name", "status", "created_at", "updated_at"]
    query = _compose_query(query, search, status)

    async def rows():
        async for sub in listmonk.iter_pages(
            listmonk.get_subscribers, per_page=100,
            query=query, list_id=list_id,
        ):
            yield sub

    # Peek one row so an empty result is still a 404 rather than a 0-byte file.
    stream = rows()
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        raise HTTPException(status_code=404, detail="No subscribers found")

    async def with_first():
        yield first
        async for sub in stream:
            yield sub

    return StreamingResponse(
        aiter_dicts_to_csv(with_first(), columns),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=subscribers_export.csv"},
    )


@router.get("/import/status")
async def get_import_status():
    return await listmonk.get_import_status()


@router.get("/import/logs")
async def get_import_logs():
    return await listmonk.get_import_logs()


@router.get("/{subscriber_id}")
async def get_subscriber(subscriber_id: int):
    return await listmonk.get_subscriber(subscriber_id)


@router.get("/{subscriber_id}/export")
async def export_subscriber(subscriber_id: int):
    return await listmonk.export_subscriber(subscriber_id)


@router.get("/{subscriber_id}/bounces")
async def get_subscriber_bounces(subscriber_id: int):
    return await listmonk.get_subscriber_bounces(subscriber_id)


@router.post("")
async def create_subscriber(data: dict):
    return await listmonk.create_subscriber(data)


# NOTE: static routes like /lists and /blocklist MUST be registered before
# /{subscriber_id}, otherwise Starlette matches the parameterized route first
# and int-parses "lists"/"blocklist" (422), making the endpoints unreachable.
@router.put("/lists")
async def modify_list_memberships(data: dict):
    return await listmonk.modify_list_memberships(data)


@router.put("/blocklist")
async def blocklist_subscribers(data: dict):
    # parse_ids: reject junk, dedupe, and cap the batch (rule 14) instead of
    # forwarding an arbitrary client payload straight upstream.
    try:
        ids = parse_ids(data.get("ids"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ids:
        raise HTTPException(status_code=400, detail="ids must be a non-empty list of subscriber ids")
    return await listmonk.blocklist_subscribers(ids)


@router.put("/{subscriber_id}")
async def update_subscriber(subscriber_id: int, data: dict):
    return await listmonk.update_subscriber(subscriber_id, data)


@router.put("/{subscriber_id}/blocklist")
async def blocklist_subscriber(subscriber_id: int):
    return await listmonk.blocklist_subscriber(subscriber_id)


@router.delete("/{subscriber_id}")
async def delete_subscriber(subscriber_id: int):
    return await listmonk.delete_subscriber(subscriber_id)


@router.delete("")
async def delete_subscribers(ids: list[int] = Query(...)):
    try:
        parsed = parse_ids(ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not parsed:
        raise HTTPException(status_code=400, detail="ids must be a non-empty list of subscriber ids")
    return await listmonk.delete_subscribers(parsed)
