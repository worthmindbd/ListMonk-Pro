"""Deliverability router for ListMonk Pro.

Provides endpoints for the pre-flight deliverability checklist and 1-click
preset configuration for Cold Email vs Newsletter modes.
"""

import logging
from fastapi import APIRouter, HTTPException, Request
from app.services.deliverability_service import (
    get_deliverability_checklist,
    apply_deliverability_preset,
)
from app.services.listmonk_client import listmonk

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
        checklist = await get_deliverability_checklist(listmonk)
        return {
            "status": "ok",
            "mode": mode,
            "checklist": checklist,
        }
    except Exception as e:
        logger.error(f"Apply preset error: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Failed to apply preset: {e}")
