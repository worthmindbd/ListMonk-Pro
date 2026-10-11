"""Shared unsubscribe log and settings storage.

Consolidates load/save operations that were previously duplicated between
imap_unsubscribe.py and link_unsubscribe.py.
"""

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from app.config import settings

LOG_FILE = settings.data_path("unsubscribe_log.json")
SETTINGS_FILE = settings.data_path("unsubscribe_settings.json")
# Authoritative dedup source for the scanners, kept independent of the display
# log so that deleting/clearing log records does not cause already-handled
# subscribers to be processed (and re-unsubscribed) again.
PROCESSED_FILE = settings.data_path("unsubscribe_processed.json")

_DEFAULT_SETTINGS = {
    "blocklist_enabled": False,
}

_log_lock = asyncio.Lock()


def _normalize_campaign_key(key: str) -> str:
    """Convert old MM/YY keys to YYYY-MM format for proper sorting."""
    if not key or '/' not in key:
        return key
    parts = key.split('/')
    if len(parts) == 2 and len(parts[0]) == 2 and len(parts[1]) == 2:
        month, year_short = parts
        return f"20{year_short}-{month}"
    return key


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
        records = json.loads(LOG_FILE.read_text())
        migrated = False
        for r in records:
            old_key = r.get("campaign_key", "")
            new_key = _normalize_campaign_key(old_key)
            if new_key != old_key:
                r["campaign_key"] = new_key
                migrated = True
        if migrated:
            save_log(records)
        return records
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, PermissionError):
        return []


def save_log(records: list[dict]) -> None:
    _atomic_write_json(LOG_FILE, records, indent=2, ensure_ascii=False)


async def append_log(new_records: list[dict]) -> None:
    """Atomically append records to the log file under lock."""
    async with _log_lock:
        existing = load_log()
        existing.extend(new_records)
        save_log(existing)


def load_processed_emails() -> set[str]:
    """Return lowercased emails of subscribers already handled by a scanner.

    Seeded from the existing log on first run so previously processed records
    are not handled twice after this file is introduced.
    """
    try:
        data = json.loads(PROCESSED_FILE.read_text())
        if isinstance(data, list):
            return {str(e).lower() for e in data if e}
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, PermissionError):
        pass

    seeded = {r["email"].lower() for r in load_log() if r.get("email")}
    if seeded:
        try:
            save_processed_emails(seeded)
        except OSError:
            pass
    return seeded


def save_processed_emails(emails: set[str]) -> None:
    _atomic_write_json(PROCESSED_FILE, sorted(emails), indent=None, ensure_ascii=True)


async def mark_processed(emails) -> None:
    """Persist emails as handled (union with the existing set)."""
    async with _log_lock:
        current = load_processed_emails()
        current.update(str(e).lower() for e in emails if e)
        save_processed_emails(current)


async def unmark_processed(emails) -> None:
    """Remove emails from the handled set so a future unsubscribe is detected."""
    async with _log_lock:
        current = load_processed_emails()
        current.difference_update(str(e).lower() for e in emails if e)
        save_processed_emails(current)


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text())
        return {**_DEFAULT_SETTINGS, **data}
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, PermissionError):
        return dict(_DEFAULT_SETTINGS)


def save_settings(data: dict) -> None:
    merged = {**load_settings(), **data}
    _atomic_write_json(SETTINGS_FILE, merged, indent=2, ensure_ascii=True)
