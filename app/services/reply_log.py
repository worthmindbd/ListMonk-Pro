"""Shared reply log and settings storage for ListMonk Pro cold outreach.

Manages persistent storage for prospect replies, sentiment classification,
and tracking state with atomic writes and thread-safety.
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from app.config import settings

LOG_FILE = settings.data_path("reply_log.json")
SETTINGS_FILE = settings.data_path("reply_settings.json")
PROCESSED_FILE = settings.data_path("reply_processed.json")

# Cap for the persisted processed-message set (mirrors bounce_processed.json):
# without it the file grows forever on a long-running installation. The list
# is kept in insertion order (oldest first), so trimming drops the oldest
# entries — messages far outside the scanner's lookback window.
MAX_PROCESSED_IDS = 5000

_DEFAULT_SETTINGS = {
    "auto_tag_leads": True,
    "lead_tag": "lead",
    "scan_interval_minutes": 15,
    # Opt-in auto-blocklisting of automated replies, decided per reply by the
    # reply scanner (see imap_reply_tracker._auto_reply_kind). "machine"
    # covers no-reply/notification senders and ticket systems — mailboxes no
    # human reads. "ooo" additionally covers out-of-office notices written by
    # a real (temporarily away) human, which is the more destructive choice.
    "auto_blocklist_machine": False,
    "auto_blocklist_ooo": False,
}

_log_lock = asyncio.Lock()


def _atomic_write_json(file_path: Path, data: Any, indent: int = 2, ensure_ascii: bool = True) -> None:
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


def load_log() -> list[dict]:
    try:
        records = json.loads(LOG_FILE.read_text(encoding="utf-8"))
        if isinstance(records, list):
            return records
        return []
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, PermissionError):
        return []


def save_log(records: list[dict]) -> None:
    _atomic_write_json(LOG_FILE, records, indent=2, ensure_ascii=False)


async def append_log(new_records: list[dict]) -> None:
    """Atomically append records to the log file under lock."""
    async with _log_lock:
        existing = load_log()
        # Prepend so newest replies appear first
        combined = new_records + existing
        save_log(combined)


async def update_reply_status(reply_id: str, new_status: str) -> bool:
    """Update status of a single reply record.

    The change is recorded as ``status_source: "manual"`` so a later
    sentiment re-classification can undo its own automatic ``lead``
    promotion without overwriting a choice a human made.

    Runs under ``_log_lock`` so concurrent scans cannot silently drop
    records between the load and save.
    """
    async with _log_lock:
        records = load_log()
        updated = False
        for r in records:
            if str(r.get("id")) == str(reply_id):
                r["status"] = new_status
                r["status_source"] = "manual"
                updated = True
                break
        if updated:
            save_log(records)
        return updated


async def update_reply_statuses(reply_ids, new_status: str) -> int:
    """Set the same status on several records with a single write.

    Returns how many records actually changed, so a bulk action over a stale
    selection reports what really happened.

    Runs under ``_log_lock`` so concurrent scans cannot silently drop
    records between the load and save.
    """
    wanted = {str(r) for r in reply_ids if r}
    if not wanted:
        return 0
    async with _log_lock:
        records = load_log()
        updated = 0
        for r in records:
            if str(r.get("id")) in wanted:
                if r.get("status") != new_status:
                    r["status"] = new_status
                    # Same provenance marker as the single-record path: a bulk
                    # status choice is a human decision too.
                    r["status_source"] = "manual"
                    updated += 1
        if updated:
            save_log(records)
        return updated


async def delete_replies(reply_ids) -> int:
    """Remove several records with a single write; returns how many went.

    Runs under ``_log_lock`` so concurrent scans cannot silently drop
    records between the load and save.
    """
    wanted = {str(r) for r in reply_ids if r}
    if not wanted:
        return 0
    async with _log_lock:
        records = load_log()
        remaining = [r for r in records if str(r.get("id")) not in wanted]
        removed = len(records) - len(remaining)
        if removed:
            save_log(remaining)
        return removed


def _load_processed_list() -> list[str]:
    """Return the persisted processed IDs in file order (oldest first)."""
    try:
        data = json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(m) for m in data if m]
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, PermissionError):
        pass
    return []


def load_processed_message_ids() -> set[str]:
    """Return set of message IDs already handled by the reply scanner."""
    try:
        data = json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(m) for m in data if m}
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, PermissionError):
        pass
    # Seed from reply_log if processed file is missing. Include both the
    # stored message_id AND a From|Date|Subject fingerprint for older
    # records that were stored without a message_id.
    records = load_log()
    ids: set[str] = set()
    for r in records:
        mid = r.get("message_id")
        if mid:
            ids.add(str(mid))
        # Fallback fingerprint for records without message_id
        from_email = r.get("from_email", "")
        date = r.get("date", "")
        subject = r.get("subject", "")
        if from_email and date:
            ids.add(f"{from_email}|{date}|{subject}")
    return ids


async def mark_processed_message_ids(message_ids) -> None:
    """Persist several handled message IDs with a single read + write.

    The reply scanner processes up to 200 messages per run; writing the whole
    file once per message made each scan O(n²) on disk I/O. The stored list is
    insertion-ordered and capped at MAX_PROCESSED_IDS (newest entries win), so
    a long-running installation cannot grow the file without bound.

    Runs under ``_log_lock`` so concurrent scans cannot interleave reads/writes
    on ``reply_processed.json``.
    """
    fresh = [str(m) for m in message_ids if m]
    if not fresh:
        return
    async with _log_lock:
        current = _load_processed_list()
        known = set(current)
        added = []
        for m in fresh:
            if m not in known:
                known.add(m)
                added.append(m)
        if not added:
            return
        current.extend(added)
        if len(current) > MAX_PROCESSED_IDS:
            current = current[-MAX_PROCESSED_IDS:]
        _atomic_write_json(PROCESSED_FILE, current)


async def mark_processed_message_id(message_id: str) -> None:
    """Backwards-compatible single-ID wrapper."""
    if not message_id:
        return
    await mark_processed_message_ids([message_id])


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            merged = dict(_DEFAULT_SETTINGS)
            merged.update(data)
            return merged
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, PermissionError):
        pass
    return dict(_DEFAULT_SETTINGS)


def save_settings(new_settings: dict) -> None:
    current = load_settings()
    current.update(new_settings)
    _atomic_write_json(SETTINGS_FILE, current)
