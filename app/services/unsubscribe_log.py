"""Shared unsubscribe log and settings storage.

Consolidates load/save operations that were previously duplicated between
imap_unsubscribe.py and link_unsubscribe.py.
"""

import asyncio
import json
import os
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

# Cap for the persisted processed-email set (mirrors bounce_processed.json):
# without it the file grows forever on a long-running installation. Entries
# are stored oldest-first, so trimming drops the oldest ones.
#
# The cap must stay far above the number of unsubscribes an installation
# accumulates, because the link scanner re-polls ListMonk on every run and a
# link-unsubscribed subscriber stays "unsubscribed" there forever. At the old
# 5000 cap, unsubscribe #5001 evicted the oldest email, the next hourly scan
# re-discovered it as new (duplicate log record, re-blocklist), which evicted
# the next one — an endless churn of duplicate records. 50k entries is about
# 1.5 MB on disk, mirroring bounce_processed.json.
MAX_PROCESSED_EMAILS = 50_000

# Message-level idempotency for the IMAP unsubscribe scanner: Message-IDs (or
# From|Date|Subject fingerprints) of every inbox message that reached a final
# outcome. The scanner re-reads its whole lookback window instead of trusting
# IMAP \Seen (AGENTS.md rule 13: a human reading the inbox would otherwise
# hide "remove me" requests forever), so this set is what keeps a message
# from being downloaded and evaluated again on every scan. Oldest-first and
# capped like bounce_processed.json, which re-reads a 30-day window too.
EXAMINED_FILE = settings.data_path("unsubscribe_examined.json")
MAX_EXAMINED_IDS = 50_000

_DEFAULT_SETTINGS = {
    "blocklist_enabled": False,
}

_log_lock = asyncio.Lock()
_migration_done = False


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
        records = json.loads(LOG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, PermissionError):
        return []
    if not isinstance(records, list):
        # A hand-edited or half-written file holding a dict/scalar would
        # otherwise raise AttributeError on the .get() below and turn every
        # /api/unsubscribes* endpoint into a 500 until fixed by hand.
        return []
    global _migration_done
    if not _migration_done:
        migrated = False
        for r in records:
            if not isinstance(r, dict):
                continue
            old_key = r.get("campaign_key", "")
            new_key = _normalize_campaign_key(old_key)
            if new_key != old_key:
                r["campaign_key"] = new_key
                migrated = True
        if migrated:
            save_log(records)
        _migration_done = True
    return records


def save_log(records: list[dict]) -> None:
    _atomic_write_json(LOG_FILE, records, indent=2, ensure_ascii=False)


async def append_log(new_records: list[dict]) -> None:
    """Atomically append records to the log file under lock."""
    async with _log_lock:
        existing = load_log()
        existing.extend(new_records)
        save_log(existing)


def _load_processed_list() -> list[str]:
    """Return the persisted processed emails in file order (oldest first)."""
    try:
        data = json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(e).lower() for e in data if e]
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, PermissionError):
        pass
    return []


def load_processed_emails() -> set[str]:
    """Return lowercased emails of subscribers already handled by a scanner.

    Seeded from the existing log on first run so previously processed records
    are not handled twice after this file is introduced.
    """
    try:
        data = json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
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


def save_processed_emails(emails: set[str], ordered_hint: list[str] | None = None) -> None:
    """Persist handled emails as an insertion-ordered, capped list.

    Callers pass a set, so chronology comes from the existing file: known
    emails keep their stored order (oldest first) and new ones are appended.
    Trimming to MAX_PROCESSED_EMAILS then drops the oldest entries instead of
    letting the file grow forever.

    ``ordered_hint`` lets a caller that has *already* read the file supply the
    stored order, avoiding a second (blocking) read of the same JSON file.
    """
    wanted = {str(e).lower() for e in emails if e}
    existing_order = ordered_hint if ordered_hint is not None else _load_processed_list()
    ordered = [e for e in existing_order if e in wanted]
    ordered.extend(sorted(wanted - set(ordered)))
    if len(ordered) > MAX_PROCESSED_EMAILS:
        ordered = ordered[-MAX_PROCESSED_EMAILS:]
    _atomic_write_json(PROCESSED_FILE, ordered, indent=None, ensure_ascii=True)


async def mark_processed(emails) -> None:
    """Persist emails as handled (union with the existing set)."""
    async with _log_lock:
        existing_order = _load_processed_list()
        if existing_order:
            # Fast path: the file already exists, so derive the set from the
            # order we just read instead of reading the same JSON twice.
            current = set(existing_order)
        else:
            # First run: fall back to the seeding path (writes the file).
            current = load_processed_emails()
            existing_order = _load_processed_list()
        current.update(str(e).lower() for e in emails if e)
        save_processed_emails(current, ordered_hint=existing_order)


async def unmark_processed(emails) -> None:
    """Remove emails from the handled set so a future unsubscribe is detected."""
    async with _log_lock:
        existing_order = _load_processed_list()
        if existing_order:
            current = set(existing_order)
        else:
            current = load_processed_emails()
            existing_order = _load_processed_list()
        current.difference_update(str(e).lower() for e in emails if e)
        save_processed_emails(current, ordered_hint=existing_order)


def load_examined_ids() -> list[str]:
    """Return the persisted examined-message identifiers, oldest first."""
    try:
        data = json.loads(EXAMINED_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(m) for m in data if m]
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, PermissionError):
        pass
    return []


def examined_state_exists() -> bool:
    """False until the first scan of a version that keeps the examined set."""
    return EXAMINED_FILE.exists()


async def mark_examined_ids(ids, create: bool = False) -> None:
    """Add message identifiers to the examined set (one read + one write).

    ``create`` writes the file even when nothing is added, which records
    that the one-time migration seeding (see the scanner) has run.
    """
    fresh = [str(i) for i in ids if i]
    if not fresh:
        if create and not EXAMINED_FILE.exists():
            async with _log_lock:
                if not EXAMINED_FILE.exists():
                    _atomic_write_json(EXAMINED_FILE, [], indent=None, ensure_ascii=True)
        return
    async with _log_lock:
        current = load_examined_ids()
        known = set(current)
        added = [i for i in dict.fromkeys(fresh) if i not in known]
        if not added:
            return
        current.extend(added)
        if len(current) > MAX_EXAMINED_IDS:
            current = current[-MAX_EXAMINED_IDS:]
        _atomic_write_json(EXAMINED_FILE, current, indent=None, ensure_ascii=True)


def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        # A hand-edited file holding a list/scalar must not 500 every scan
        # ({**DEFAULTS, **[...]} raises TypeError).
        if not isinstance(data, dict):
            return dict(_DEFAULT_SETTINGS)
        return {**_DEFAULT_SETTINGS, **data}
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, PermissionError):
        return dict(_DEFAULT_SETTINGS)


def save_settings(data: dict) -> None:
    merged = {**load_settings(), **data}
    _atomic_write_json(SETTINGS_FILE, merged, indent=2, ensure_ascii=True)
