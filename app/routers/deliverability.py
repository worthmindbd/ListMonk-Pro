"""Deliverability router for ListMonk Pro.

Provides endpoints for the pre-flight deliverability checklist and 1-click
preset configuration for Cold Email vs Newsletter modes.
"""

import asyncio
import logging
from fastapi import APIRouter, HTTPException, Request
from app.services.bulk_ops import parse_ids
from app.services.deliverability_service import (
    get_deliverability_checklist,
    apply_deliverability_preset,
    update_deliverability_settings,
    validate_settings_patch,
)
from app.services.listmonk_client import listmonk
from app.services.template_unsubscribe import set_unsubscribe_links

logger = logging.getLogger("deliverability_router")
router = APIRouter()


@router.get("/checklist")
async def deliverability_checklist():
    """Run deliverability checks against current Listmonk settings."""
    try:
        result = await get_deliverability_checklist(listmonk)
    except Exception as e:
        logger.error(f"Deliverability check error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Deliverability check failed: {e}")

    # The service reports an upstream outage in-band ("overall_status":
    # "error"); rule 7 still demands a failure status code so monitoring and
    # uptime probes see the outage instead of a 200.
    if result.get("overall_status") == "error":
        raise HTTPException(
            status_code=502,
            detail=f"Deliverability check failed: {result.get('error', 'ListMonk unreachable')}",
        )
    return result


# ListMonk reloads itself after a settings PUT; a GET issued in that window
# can fail. Retry briefly so the response carries the post-change audit.
POST_WRITE_RETRIES = 3
POST_WRITE_DELAY = 1.5


async def _checklist_after_write() -> dict:
    checklist = await get_deliverability_checklist(listmonk)
    for _ in range(POST_WRITE_RETRIES - 1):
        if checklist.get("overall_status") != "error":
            break
        await asyncio.sleep(POST_WRITE_DELAY)
        checklist = await get_deliverability_checklist(listmonk)
    return checklist


async def _json_body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    return data


@router.put("/settings")
async def update_settings(request: Request):
    """Change individual deliverability settings (whitelisted keys only)."""
    data = await _json_body(request)
    try:
        patch = validate_settings_patch(data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        await update_deliverability_settings(listmonk, patch)
    except Exception as e:
        logger.error(f"Update deliverability settings error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Failed to update Listmonk settings: {e}")

    return {"status": "ok", "applied": patch, "checklist": await _checklist_after_write()}


@router.put("/unsubscribe-link")
async def toggle_unsubscribe_link(request: Request):
    """Show or hide the footer unsubscribe link in campaign templates.

    This is the visible link in the email body — unlike
    ``privacy.unsubscribe_header``, which only sets the List-Unsubscribe header.
    """
    data = await _json_body(request)
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled must be true or false")
    try:
        template_ids = parse_ids(data.get("template_ids"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        result = await set_unsubscribe_links(listmonk, enabled, template_ids or None)
    except Exception as e:
        logger.error(f"Unsubscribe link toggle error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Failed to update templates: {e}")

    if result["errors"] and not result["updated"]:
        names = ", ".join(str(f.get("name") or f.get("id")) for f in result["failed"])
        raise HTTPException(status_code=502, detail=f"No template could be updated ({names})")

    return {"status": "ok", **result, "checklist": await get_deliverability_checklist(listmonk)}


@router.post("/apply-preset")
async def apply_preset(request: Request):
    """Apply an optimized deliverability preset to Listmonk ('cold_email' or 'newsletter')."""
    try:
        data = await request.json()
        mode = data.get("mode", "cold_email")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if mode not in {"cold_email", "newsletter"}:
        raise HTTPException(status_code=400, detail="mode must be 'cold_email' or 'newsletter'")

    try:
        await apply_deliverability_preset(listmonk, mode)
        checklist = await _checklist_after_write()
        return {
            "status": "ok",
            "mode": mode,
            "checklist": checklist,
        }
    except Exception as e:
        logger.error(f"Apply preset error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Failed to apply preset: {e}")
