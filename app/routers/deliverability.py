"""Deliverability router for ListMonk Pro.

Provides endpoints for the pre-flight deliverability checklist and 1-click
preset configuration for Cold Email vs Newsletter modes.
"""

import asyncio
import logging

import httpx
from fastapi import APIRouter, HTTPException, Path, Request
from app.services.bulk_ops import parse_ids
from app.services.deliverability_service import (
    get_deliverability_checklist,
    apply_deliverability_preset,
    update_deliverability_settings,
    validate_settings_patch,
)
from app.services.email_format import FormatError, list_email_formats, set_email_format
from app.services.listmonk_client import listmonk
from app.services.template_unsubscribe import describe_error, set_unsubscribe_links

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
        # Templates are read in the same pass; a reload window shows up as
        # an "unknown" footer-link state rather than an error status.
        link_state = ((checklist.get("raw_settings") or {}).get("unsubscribe_link") or {}).get("state")
        if checklist.get("overall_status") != "error" and link_state != "unknown":
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
        raise HTTPException(status_code=502, detail=f"Failed to update templates: {describe_error(e)}")

    if result["errors"] and not result["updated"]:
        names = ", ".join(str(f.get("name") or f.get("id")) for f in result["failed"])
        raise HTTPException(status_code=502, detail=f"No template could be updated ({names})")

    return {"status": "ok", **result, "checklist": await get_deliverability_checklist(listmonk)}


@router.get("/email-format")
async def email_formats():
    """Campaigns that can still send, each with its HTML/plain format."""
    try:
        return {"campaigns": await list_email_formats(listmonk)}
    except Exception as e:
        logger.error(f"Email format listing error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Could not read campaigns: {describe_error(e)}")


@router.put("/email-format/{campaign_id}")
async def switch_email_format(request: Request, campaign_id: int = Path(..., ge=1)):
    """Switch one campaign between its HTML design and plain-text version."""
    data = await _json_body(request)
    fmt = data.get("format")
    if fmt not in ("html", "plain"):
        raise HTTPException(status_code=400, detail="format must be 'html' or 'plain'")
    try:
        return await set_email_format(listmonk, campaign_id, fmt)
    except FormatError as e:
        raise HTTPException(status_code=e.status, detail=str(e))
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Campaign not found")
        logger.error(f"Email format switch error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Failed to switch format: {describe_error(e)}")
    except Exception as e:
        logger.error(f"Email format switch error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Failed to switch format: {describe_error(e)}")


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
