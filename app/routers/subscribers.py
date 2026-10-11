from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse
from typing import Optional
from app.services.listmonk_client import listmonk
from app.services.export_service import aiter_dicts_to_csv

router = APIRouter()

# Pagination bounds (the UI requests at most 50).
MAX_PAGE = 100
MAX_PER_PAGE = 200

# Export streams pages lazily; cap the walk so a misbehaving API cannot hold
# the connection open indefinitely. 100 pages x 100 rows = 10k subscribers.
EXPORT_MAX_PAGES = 100


@router.get("")
async def get_subscribers(page: int = Query(1, ge=1, le=MAX_PAGE),
                          per_page: int = Query(50, ge=1, le=MAX_PER_PAGE),
                          query: str = "", list_id: Optional[int] = None):
    return await listmonk.get_subscribers(page, per_page, query, list_id)


@router.get("/export-all")
async def export_all_subscribers(query: str = "", list_id: Optional[int] = None):
    """Stream every subscriber as CSV, page by page.

    Rows are pulled from ListMonk lazily so peak memory stays at one page
    regardless of list size. 404 when the filter matches nothing.
    """
    columns = ["id", "email", "name", "status", "created_at", "updated_at"]

    async def rows():
        async for sub in listmonk.iter_pages(
            listmonk.get_subscribers, per_page=100,
            max_pages=EXPORT_MAX_PAGES,
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
    return await listmonk.blocklist_subscribers(data.get("ids", []))


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
    return await listmonk.delete_subscribers(ids)
