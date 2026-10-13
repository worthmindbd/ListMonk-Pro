from fastapi import APIRouter, HTTPException, Request

from app.services.app_settings import VALID_MODES, load_settings, set_mode

router = APIRouter()


@router.get("")
async def get_app_settings():
    """Return the installation-wide mode selection state.

    `mode_selected` tells the frontend whether the onboarding modal still
    needs to be shown (first login after install only, not per browser).
    """
    return load_settings()


@router.put("")
async def update_app_settings(request: Request):
    """Persist the operating mode chosen during onboarding or the switcher."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Settings must be a JSON object")
    mode = data.get("mode")
    if mode not in VALID_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"mode must be one of: {', '.join(sorted(VALID_MODES))}",
        )
    return await set_mode(mode)
