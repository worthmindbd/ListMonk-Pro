"""Subscribers that were blocklisted on purpose through this app.

Auto-unblock reverses *bounce-driven* blocklistings of contacts who later
engaged. ListMonk does not record why a subscriber is blocklisted, so a
deliberate one (the Leads / Replies / Email Validation "Blocklist" actions,
the reply scanner's opt-in auto-blocklist, the scheduled cleanup) looked the
same as soon as the contact also had a bounce record, and was silently undone
six hours later. This registry is the missing "why": ids listed here are never
re-enabled automatically.

State: ``manual_blocklist.json`` in DATA_DIR, written atomically::

    {"ids": [...],          subscriber ids blocklisted on purpose (oldest first)
     "emails": [...],       addresses blocklisted on purpose by e-mail only
                            (a blocklist-mode CSV import: ListMonk imports in
                            the background, so no ids are known)
     "unblocked": {id: ts}} when a subscriber was re-enabled by hand, so the
                            cleanup's hard-bounce rule does not blocklist them
                            again over a bounce from before that decision

The original format (a bare list of ids) is still read.
"""

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone

from app.config import settings

logger = logging.getLogger("blocklist_registry")

REGISTRY_FILE = settings.data_path("manual_blocklist.json")
MAX_IDS = 50_000

# Callers run on the event loop and in worker threads.
_LOCK = threading.Lock()


def _valid(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _empty() -> dict:
    return {"ids": [], "emails": [], "unblocked": {}}


def _load_state() -> dict:
    try:
        data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, PermissionError):
        return _empty()
    if isinstance(data, list):  # original format: ids only
        return {**_empty(), "ids": [i for i in data if _valid(i)]}
    if not isinstance(data, dict):
        return _empty()
    unblocked = data.get("unblocked")
    return {
        "ids": [i for i in data.get("ids") or [] if _valid(i)],
        "emails": [e for e in data.get("emails") or [] if isinstance(e, str) and "@" in e],
        "unblocked": {
            k: v for k, v in (unblocked.items() if isinstance(unblocked, dict) else [])
            if str(k).isdigit() and isinstance(v, str)
        },
    }


def _save_state(state: dict) -> None:
    unblocked = list(state["unblocked"].items())[-MAX_IDS:]
    payload = {
        "ids": state["ids"][-MAX_IDS:],
        "emails": state["emails"][-MAX_IDS:],
        "unblocked": dict(unblocked),
    }
    tmp_path = None
    try:
        REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=REGISTRY_FILE.parent, prefix=REGISTRY_FILE.name, suffix=".tmp",
            delete=False, encoding="utf-8",
        ) as f:
            tmp_path = f.name
            json.dump(payload, f)
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
    return set(_load_state()["ids"])


def load_emails() -> set[str]:
    """Addresses blocklisted on purpose by e-mail (blocklist-mode imports)."""
    return set(_load_state()["emails"])


def load_unblocked() -> dict[int, datetime]:
    """Subscriber id -> when it was last re-enabled by hand."""
    out = {}
    for key, ts in _load_state()["unblocked"].items():
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        out[int(key)] = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return out


def mark(ids) -> None:
    """Record deliberate blocklistings (one read + one write)."""
    fresh = [i for i in (ids or []) if _valid(i)]
    if not fresh:
        return
    with _LOCK:
        state = _load_state()
        known = set(state["ids"])
        added = [i for i in dict.fromkeys(fresh) if i not in known]
        # Blocklisted again on purpose: an earlier manual unblock no longer
        # describes them.
        cleared = [str(i) for i in fresh if str(i) in state["unblocked"]]
        if added or cleared:
            state["ids"].extend(added)
            for key in cleared:
                state["unblocked"].pop(key, None)
            _save_state(state)


def mark_emails(emails) -> None:
    """Record deliberate blocklistings known only by address."""
    fresh = [e.strip().lower() for e in (emails or []) if isinstance(e, str) and "@" in e]
    if not fresh:
        return
    with _LOCK:
        state = _load_state()
        known = set(state["emails"])
        added = [e for e in dict.fromkeys(fresh) if e not in known]
        if added:
            state["emails"].extend(added)
            _save_state(state)


def unmark(ids) -> None:
    """Forget ids (the user re-enabled them through this app)."""
    drop = {i for i in (ids or []) if _valid(i)}
    if not drop:
        return
    with _LOCK:
        state = _load_state()
        kept = [i for i in state["ids"] if i not in drop]
        if len(kept) != len(state["ids"]):
            state["ids"] = kept
            _save_state(state)


def record_unblock(ids, emails=()) -> None:
    """A human re-enabled these subscribers: forget the deliberate blocklisting
    and remember when, so bounces from before this decision are not held
    against them again (see email_cleanup._collect_hard_bounced)."""
    valid = [i for i in (ids or []) if _valid(i)]
    drop_emails = {e.strip().lower() for e in (emails or []) if isinstance(e, str)}
    if not valid and not drop_emails:
        return
    stamp = datetime.now(timezone.utc).isoformat()
    with _LOCK:
        state = _load_state()
        state["ids"] = [i for i in state["ids"] if i not in set(valid)]
        state["emails"] = [e for e in state["emails"] if e not in drop_emails]
        for i in valid:
            state["unblocked"].pop(str(i), None)
            state["unblocked"][str(i)] = stamp
        _save_state(state)
