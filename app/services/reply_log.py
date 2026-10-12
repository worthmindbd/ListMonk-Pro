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

_DEFAULT_SETTINGS = {
    "auto_tag_leads": True,
    "lead_tag": "lead",
    "scan_interval_minutes": 15,
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


def update_reply_status(reply_id: str, new_status: str) -> bool:
    """Update status of a single reply record."""
    records = load_log()
    updated = False
    for r in records:
        if str(r.get("id")) == str(reply_id):
            r["status"] = new_status
            updated = True
            break
    if updated:
        save_log(records)
    return updated


def update_reply_statuses(reply_ids, new_status: str) -> int:
    """Set the same status on several records with a single write.

    Returns how many records actually changed, so a bulk action over a stale
    selection reports what really happened.
    """
    wanted = {str(r) for r in reply_ids if r}
    if not wanted:
        return 0
    records = load_log()
    updated = 0
    for r in records:
        if str(r.get("id")) in wanted:
            if r.get("status") != new_status:
                r["status"] = new_status
                updated += 1
    if updated:
        save_log(records)
    return updated


def delete_replies(reply_ids) -> int:
    """Remove several records with a single write; returns how many went."""
    wanted = {str(r) for r in reply_ids if r}
    if not wanted:
        return 0
    records = load_log()
    remaining = [r for r in records if str(r.get("id")) not in wanted]
    removed = len(records) - len(remaining)
    if removed:
        save_log(remaining)
    return removed


def load_processed_message_ids() -> set[str]:
    """Return set of message IDs already handled by the reply scanner."""
    try:
        data = json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(m) for m in data if m}
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, PermissionError):
        pass
    # Seed from reply_log if processed file is missing
    records = load_log()
    return {str(r.get("message_id")) for r in records if r.get("message_id")}


def mark_processed_message_ids(message_ids) -> None:
    """Persist several handled message IDs with a single read + write.

    The reply scanner processes up to 200 messages per run; writing the whole
    file once per message made each scan O(n²) on disk I/O.
    """
    fresh = {str(m) for m in message_ids if m}
    if not fresh:
        return
    current = load_processed_message_ids()
    if fresh <= current:
        return
    current.update(fresh)
    _atomic_write_json(PROCESSED_FILE, sorted(current))


def mark_processed_message_id(message_id: str) -> None:
    """Backwards-compatible single-ID wrapper."""
    if not message_id:
        return
    mark_processed_message_ids([message_id])


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
