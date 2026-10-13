from fastapi import APIRouter, Query, HTTPException
from app.services.listmonk_client import listmonk
from app.services.bulk_ops import parse_ids, run_bulk

router = APIRouter()

# Pagination bounds (the UI requests at most 100).
MAX_PAGE = 100
MAX_PER_PAGE = 200


@router.get("")
async def get_lists(page: int = Query(1, ge=1, le=MAX_PAGE),
                    per_page: int = Query(50, ge=1, le=MAX_PER_PAGE),
                    query: str = "", status: str = "",
                    order_by: str = "created_at", order: str = "DESC",
                    minimal: bool = False):
    return await listmonk.get_lists(page, per_page, query, status,
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
    result = await run_bulk(parsed, listmonk.delete_list)
    return {
        "deleted": result["ok"],
        "errors": result["errors"],
        "failed": result["failed"],
    }


@router.delete("/{list_id}")
async def delete_list(list_id: int):
    return await listmonk.delete_list(list_id)