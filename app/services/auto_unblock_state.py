"""
Auto-unblock settings and run history (``auto_unblock.json`` in DATA_DIR).

The six-hourly auto-unblock used to be invisible: always on, no record of
when it last ran or whom it re-enabled. This keeps:

* ``enabled`` — the background loop skips its runs while off (default on, the
  behaviour before the switch existed); "Run now" still works;
* ``last_run`` — when, how (``schedule`` / ``manual``) and the counts;
* ``history`` — the addresses re-enabled, newest first, capped at
  ``HISTORY_LIMIT``.
"""

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

from app.config import settings

logger = logging.getLogger("auto_unblock_state")

STATE_FILE = settings.data_path("auto_unblock.json")
HISTORY_LIMIT = 500

# Serialises load → mutate → save (reset in tests/conftest.py, rule 11).
_STATE_LOCK = asyncio.Lock()


def _default() -> dict:
    return {"enabled": True, "last_run": None, "history": []}


def load_state() -> dict:
    state = _default()
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return state
    except Exception as e:
        logger.error(f"Failed to load auto-unblock state: {e}")
        return state
    if not isinstance(data, dict):
        return state
    if isinstance(data.get("enabled"), bool):
        state["enabled"] = data["enabled"]
    last = data.get("last_run")
    if isinstance(last, dict) and isinstance(last.get("at"), str):
        state["last_run"] = {
            "at": last["at"],
            "trigger": last.get("trigger") if last.get("trigger") in ("schedule", "manual") else "schedule",
            "success": last.get("success") if isinstance(last.get("success"), int) else 0,
            "failed": last.get("failed") if isinstance(last.get("failed"), int) else 0,
            "bounces_deleted": last.get("bounces_deleted") if isinstance(last.get("bounces_deleted"), int) else 0,
            "error": last.get("error") if isinstance(last.get("error"), str) else None,
        }
    hist = data.get("history")
    if isinstance(hist, list):
        state["history"] = [
            {"email": h["email"], "at": h["at"], "trigger": h.get("trigger", "schedule")}
            for h in hist
            if isinstance(h, dict) and isinstance(h.get("email"), str) and isinstance(h.get("at"), str)
        ][:HISTORY_LIMIT]
    return state


def save_state(state: dict) -> None:
    """Atomic write (tempfile + fsync + replace)."""
    temp_path = None
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=STATE_FILE.parent, prefix="auto_unblock_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, STATE_FILE)
        temp_path = None
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


async def set_enabled(enabled: bool) -> dict:
    async with _STATE_LOCK:
        state = load_state()
        state["enabled"] = enabled
        save_state(state)
        return state


async def record_run(result: Optional[dict], trigger: str, error: Optional[str] = None,
                     now: Optional[datetime] = None) -> None:
    """Store a run's outcome and the addresses it re-enabled. Never raises:
    bookkeeping must not turn a successful unblock into an error."""
    at = (now or datetime.now(timezone.utc)).isoformat()
    result = result or {}
    try:
        async with _STATE_LOCK:
            state = load_state()
            state["last_run"] = {
                "at": at,
                "trigger": trigger,
                "success": int(result.get("success") or 0),
                "failed": int(result.get("failed") or 0),
                "bounces_deleted": int(result.get("bounces_deleted") or 0),
                "error": error,
            }
            added = [{"email": e, "at": at, "trigger": trigger}
                     for e in (result.get("unblocked") or []) if isinstance(e, str)]
            state["history"] = (added + state["history"])[:HISTORY_LIMIT]
            save_state(state)
    except Exception as e:
        logger.error(f"Failed to record the auto-unblock run: {e}")
