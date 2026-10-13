"""Installation-wide app settings persisted in DATA_DIR.

The onboarding "pick a mode" choice used to live only in the browser's
localStorage, so every new browser, device, incognito session, or cleared
cookie jar re-asked a question the user had already answered. Storing it
server-side makes the choice part of the installation: the onboarding modal
appears exactly once, on first login after install.
"""

import asyncio
import json
import logging
import os
import tempfile
from typing import Any

from app.config import settings

logger = logging.getLogger("app_settings")

APP_SETTINGS_FILE = settings.data_path("app_settings.json")

VALID_MODES = {"newsletter", "cold_email"}

_DEFAULT_SETTINGS = {
    "mode": "newsletter",
    "mode_selected": False,
}

_lock = asyncio.Lock()


def _atomic_write_json(file_path, data: Any, indent: int = 2, ensure_ascii: bool = True) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=file_path.parent, delete=False, encoding="utf-8") as f:
            temp_file = f.name
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, file_path)
    except Exception:
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass
        raise


def load_settings() -> dict:
    try:
        data = json.loads(APP_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, PermissionError):
        return dict(_DEFAULT_SETTINGS)
    if not isinstance(data, dict):
        # A file holding a list/scalar (hand-edited or half-migrated) parses as
        # valid JSON but cannot be merged; falling back to the defaults keeps
        # GET /api/app-settings — and with it onboarding — working.
        logger.warning(
            "app_settings.json holds a %s instead of an object; using defaults",
            type(data).__name__,
        )
        return dict(_DEFAULT_SETTINGS)
    state = {**_DEFAULT_SETTINGS, **data}
    # A hand-edited or partially written file must not trap the dashboard in a
    # broken mode: fall back to the default instead.
    if state.get("mode") not in VALID_MODES:
        state["mode"] = _DEFAULT_SETTINGS["mode"]
    return state


async def set_mode(mode: str) -> dict:
    """Persist the operating mode and mark onboarding as completed."""
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode: {mode!r}")
    async with _lock:
        state = {**load_settings(), "mode": mode, "mode_selected": True}
        _atomic_write_json(APP_SETTINGS_FILE, state)
    return state
