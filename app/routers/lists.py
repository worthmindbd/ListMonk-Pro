from fastapi import APIRouter, Query, HTTPException
from app.services import blocklist_registry, email_cleanup
from app.services.listmonk_client import listmonk
from app.services.search_query import plain_search_term
from app.services.bulk_ops import parse_ids, run_bulk

router = APIRouter()

# Pagination bounds (the UI requests at most 100).
MAX_PAGE = 100
MAX_PER_PAGE = 200

SUBSCRIBER_PAGE_SIZE = 500
DELETE_CHUNK_SIZE = 100
MAX_SUBSCRIBER_PAGES = 1000


async def _collect_list_subscribers(list_id: int) -> tuple[list[int], list[int]]:
    """Return ``(delete_ids, dismiss_ids)`` for ``list_id``.

    All pages are read before any deletion because ListMonk paginates
    ``/api/subscribers`` by SQL OFFSET. Blocklisted subscribers (by ListMonk
    status or ``blocklist_registry``) are excluded from ``delete_ids`` so they
    stay on the blacklist after the list is removed.
    """
    manual_blocked = blocklist_registry.load()
    delete_ids: list[int] = []
    dismiss_ids: list[int] = []
    seen: set[int] = set()

    page = 1
    while page <= MAX_SUBSCRIBER_PAGES:
        res = await listmonk.get_subscribers(
            page=page, per_page=SUBSCRIBER_PAGE_SIZE, list_id=list_id,
        )
        data = res.get("data") if isinstance(res, dict) else None
        data = data if isinstance(data, dict) else {}
        rows = data.get("results") if isinstance(data.get("results"), list) else []
        if not rows:
            break

        for row in rows:
            if not isinstance(row, dict):
                continue
            sid = row.get("id")
            if not isinstance(sid, int) or isinstance(sid, bool) or sid < 1 or sid in seen:
                continue
            seen.add(sid)
            dismiss_ids.append(sid)
            status = str(row.get("status") or "").strip().lower()
            if status != "blocklisted" and sid not in manual_blocked:
                delete_ids.append(sid)

        total = data.get("total")
        if (isinstance(total, (int, float))
                and not isinstance(total, bool)
                and page * SUBSCRIBER_PAGE_SIZE >= total):
            break
        if len(rows) < SUBSCRIBER_PAGE_SIZE and total is None:
            break
        page += 1

    return delete_ids, dismiss_ids


async def _delete_list_and_subscribers(list_id: int) -> dict:
    """Delete ``list_id`` and all of its non-blocklisted subscribers.

    Subscribers are collected before the list is deleted (once the list is
    gone, ListMonk drops its ``subscriber_lists`` rows). Blocklisted
    subscribers are never deleted so they remain on ListMonk's blocklist, only
    unlinked from the deleted list. Any stored Email Validation findings for
    the list's subscribers are dismissed.
    """
    delete_ids, dismiss_ids = await _collect_list_subscribers(list_id)
    result = await listmonk.delete_list(list_id)

    deleted_ids: list[int] = []
    try:
        for i in range(0, len(delete_ids), DELETE_CHUNK_SIZE):
            chunk = delete_ids[i:i + DELETE_CHUNK_SIZE]
            await listmonk.delete_subscribers(chunk)
            deleted_ids.extend(chunk)
    finally:
        delete_set = set(delete_ids)
        to_dismiss = list(
            dict.fromkeys([*deleted_ids, *(sid for sid in dismiss_ids if sid not in delete_set)])
        )
        if to_dismiss:
            await email_cleanup.dismiss_results(to_dismiss)

    return result


@router.get("")
async def get_lists(page: int = Query(1, ge=1, le=MAX_PAGE),
                    per_page: int = Query(50, ge=1, le=MAX_PER_PAGE),
                    query: str = "", search: str = "", status: str = "",
                    order_by: str = "created_at", order: str = "DESC",
                    minimal: bool = False):
    # Validate sort parameters against known columns to prevent injection.
    _VALID_LIST_ORDER_BY = {"created_at", "updated_at", "name", "type"}
    if order_by not in _VALID_LIST_ORDER_BY:
        raise HTTPException(status_code=400, detail=f"Invalid order_by: {order_by}")
    if order.upper() not in ("ASC", "DESC"):
        raise HTTPException(status_code=400, detail=f"Invalid order: {order}")
    order = order.upper()
    # ListMonk's `query` on /api/lists is a plain name search, NOT SQL (only
    # /api/subscribers takes a SQL expression). Wrapping the term in an ILIKE
    # clause made ListMonk answer 500 for every search, so the text is passed
    # through as typed; ListMonk binds it as a query parameter itself.
    combined = plain_search_term(search or query)
    return await listmonk.get_lists(page, per_page, combined, status,
                                    order_by, order, minimal)


@router.get("/{list_id}")
async def get_list(list_id: int):
    return await listmonk.get_list(list_id)


@router.post("")
async def create_list(data: dict):
    return await listmonk.create_list(data)


@router.put("/{list_id}")
async def update_list(list_id: int, data: dict):
    return await listmonk.update_list(list_id, data)


@router.delete("")
async def delete_lists(ids: list[int] = Query(...)):
    """Delete several lists in one request.

    ListMonk deletes one list per call, so the loop happens here — bounded so
    selecting a page of lists cannot open one upstream request per row at once.
    parse_ids enforces the shared cap and rejects non-positive ids (rule 14).
    """
    try:
        parsed = parse_ids(ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not parsed:
        raise HTTPException(status_code=400, detail="No list ids provided")
    # concurrency=1: two lists in the same batch may share subscribers, so
    # deleting one list's subscribers while another list is still paginating by
    # SQL OFFSET would shift its pages.
    result = await run_bulk(parsed, _delete_list_and_subscribers, concurrency=1)
    return {
        "deleted": result["ok"],
        "errors": result["errors"],
        "failed": result["failed"],
    }


@router.delete("/{list_id}")
async def delete_list(list_id: int):
    return await _delete_list_and_subscribers(list_id)