from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import HTMLResponse
from app.services.listmonk_client import listmonk
from app.services.bulk_ops import parse_ids, run_bulk

router = APIRouter()


@router.get("")
async def get_templates():
    return await listmonk.get_templates()


_SANDBOX_HEADERS = {
    "Content-Security-Policy": "sandbox",
    "X-Content-Type-Options": "nosniff",
}
_TEMPLATE_TYPES = {"campaign", "campaign_visual", "tx"}
# A template is a page of HTML; anything far beyond this is not one.
MAX_PREVIEW_BODY = 2_000_000


# Static route before /{template_id} (Starlette would int-parse "preview").
@router.post("/preview")
async def preview_template_body(data: dict):
    """Render an unsaved template body with ListMonk's sample data, so the
    editor can show a live preview while typing."""
    body = data.get("body")
    template_type = data.get("type") or "campaign"
    if not isinstance(body, str):
        raise HTTPException(status_code=400, detail="body must be a string")
    if len(body) > MAX_PREVIEW_BODY:
        raise HTTPException(status_code=413, detail="Template body is too large to preview")
    if template_type not in _TEMPLATE_TYPES:
        raise HTTPException(status_code=400, detail="Unknown template type")
    resp = await listmonk.preview_template_body(body, template_type)
    return HTMLResponse(content=resp.text, headers=_SANDBOX_HEADERS)


@router.get("/{template_id}")
async def get_template(template_id: int):
    return await listmonk.get_template(template_id)


@router.get("/{template_id}/preview")
async def preview_template(template_id: int):
    """Rendered template HTML (ListMonk fills in a sample campaign/subscriber).

    The raw body is full of ``{{ … }}`` template actions, so showing it as-is
    is not a preview. Untrusted HTML: sandboxed like the campaign preview.
    """
    resp = await listmonk.preview_template(template_id)
    return HTMLResponse(
        content=resp.text,
        headers={
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


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
