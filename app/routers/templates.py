from fastapi import APIRouter, Query, HTTPException
from app.services.listmonk_client import listmonk
from app.services.bulk_ops import parse_ids, run_bulk

router = APIRouter()


@router.get("")
async def get_templates():
    return await listmonk.get_templates()


@router.get("/{template_id}")
async def get_template(template_id: int):
    return await listmonk.get_template(template_id)


@router.post("")
async def create_template(data: dict):
    return await listmonk.create_template(data)


@router.put("/{template_id}")
async def update_template(template_id: int, data: dict):
    return await listmonk.update_template(template_id, data)


@router.put("/{template_id}/default")
async def set_default_template(template_id: int):
    return await listmonk.set_default_template(template_id)


@router.delete("")
async def delete_templates(ids: list[int] = Query(...)):
    """Delete several templates in one request.

    ListMonk deletes one template per call; the bounded loop keeps a page-wide
    selection from opening every request at once. parse_ids enforces the
    shared cap and rejects non-positive ids (rule 14).
    """
    try:
        parsed = parse_ids(ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not parsed:
        raise HTTPException(status_code=400, detail="No template ids provided")
    result = await run_bulk(parsed, listmonk.delete_template)
    return {
        "deleted": result["ok"],
        "errors": result["errors"],
        "failed": result["failed"],
    }


@router.delete("/{template_id}")
async def delete_template(template_id: int):
    return await listmonk.delete_template(template_id)
