"""Subscribers that were blocklisted on purpose through this app.

Auto-unblock reverses *bounce-driven* blocklistings of contacts who later
engaged. ListMonk does not record why a subscriber is blocklisted, so a
deliberate one (the Leads / Replies / Email Validation "Blocklist" actions,
the reply scanner's opt-in auto-blocklist, the scheduled cleanup) looked the
same as soon as the contact also had a bounce record, and was silently undone
six hours later. This registry is the missing "why": ids listed here are never
re-enabled automatically.

State: ``manual_blocklist.json`` in DATA_DIR, a capped list of subscriber ids
(oldest first), written atomically.
"""

import json
import logging
import os
import tempfile
import threading

from app.config import settings

logger = logging.getLogger("blocklist_registry")

REGISTRY_FILE = settings.data_path("manual_blocklist.json")
MAX_IDS = 50_000

# Callers run on the event loop and in worker threads.
_LOCK = threading.Lock()


def _valid(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _load_list() -> list[int]:
    try:
        data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, PermissionError):
        return []
    return [i for i in data if _valid(i)] if isinstance(data, list) else []


def _save_list(ids: list[int]) -> None:
    tmp_path = None
    try:
        REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=REGISTRY_FILE.parent, prefix=REGISTRY_FILE.name, suffix=".tmp",
            delete=False, encoding="utf-8",
        ) as f:
            tmp_path = f.name
            json.dump(ids[-MAX_IDS:], f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, REGISTRY_FILE)
        tmp_path = None
    except OSError as e:
        # Best effort: losing an entry only means auto-unblock may judge that
        # contact by its engagement again, never a failed blocklist action.
        logger.warning(f"Could not persist the deliberate-blocklist registry: {e}")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def load() -> set[int]:
    """Ids of subscribers blocklisted on purpose through this app."""
    return set(_load_list())


def mark(ids) -> None:
    """Record deliberate blocklistings (one read + one write)."""
    fresh = [i for i in (ids or []) if _valid(i)]
    if not fresh:
        return
    with _LOCK:
        current = _load_list()
        known = set(current)
        added = [i for i in dict.fromkeys(fresh) if i not in known]
        if added:
            _save_list(current + added)


def unmark(ids) -> None:
    """Forget ids (the user re-enabled them through this app)."""
    drop = {i for i in (ids or []) if _valid(i)}
    if not drop:
        return
    with _LOCK:
        current = _load_list()
        kept = [i for i in current if i not in drop]
        if len(kept) != len(current):
            _save_list(kept)
