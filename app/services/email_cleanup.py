"""
Subscriber scans and the scheduled "dead email" cleanup.

Two jobs share one engine (``_execute``):

* ``scan``    validates every *enabled* subscriber (optionally one list) and
              stores the problems for the Email Validation page. Changes
              nothing in ListMonk.
* ``cleanup`` does the same and then **blocklists** the dead addresses. It is
              what the Settings → Email Cleanup schedule (daily / weekly /
              monthly) and its "Run now" button start.

What counts as dead
-------------------
Only *definitive* findings: a malformed address, a domain that does not exist,
a domain with no mail server / a Null MX, and subscribers with a hard bounce
record (openers excluded, exactly like the Bounces page). Disposable domains,
role accounts and typos are reported (``risky``) but never cleaned, and a
transient DNS failure (``unknown``) never is either.

Safety rails (cleanup only)
---------------------------
* The DNS resolver must demonstrably work (a well-known domain resolves),
  otherwise the run fails without touching anyone.
* If the run would blocklist more than ``HOLD_FRACTION`` of the checked
  subscribers it is *held*: nothing is blocklisted and the user reviews the
  results on the page. A resolver or data problem looks exactly like that.
* Candidates are collected first and blocklisted afterwards in chunks; nothing
  is mutated while pages are still being read (ListMonk paginates by offset).

State (both in ``DATA_DIR``, atomic writes)
-------------------------------------------
``email_cleanup.json``            settings, schedule bookkeeping, last runs
``email_validation_results.json`` the problem addresses of the latest scans
                                  (valid ones are counted, never stored)
"""

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import settings
from app.services import blocklist_registry
from app.services import email_validation as ev
from app.services.bounce_filters import filter_bounces_excluding_openers
from app.services.task_utils import spawn

logger = logging.getLogger("email_cleanup")

CLEANUP_FILE = settings.data_path("email_cleanup.json")
RESULTS_FILE = settings.data_path("email_validation_results.json")

PAGE_SIZE = 500
BOUNCE_PAGE_SIZE = 500
CLEAN_CHUNK = 100                 # subscribers per blocklist call
RESULTS_CAP = 50_000              # problem addresses kept (same cap as the other state sets)
RECENT_CLEANED_LIMIT = 100
# A cleanup that would remove more than this share of the checked subscribers
# is held for review (only judged once the sample is big enough to mean much).
HOLD_FRACTION = 0.20
HOLD_MIN_SAMPLE = 50
# After a failed scheduled run, wait this long before trying again.
RETRY_AFTER_FAILURE = timedelta(hours=1)

FREQUENCIES = ("daily", "weekly", "monthly")
DEFAULT_TIMEZONE = "UTC"
SETTING_KEYS = (
    "enabled", "frequency", "time", "weekday", "day_of_month", "timezone",
    "clean_invalid", "clean_hard_bounces",
)
# Changing any of these moves the schedule, so the pending slot is re-baselined
# (otherwise saving "daily at 03:00" at 10:00 would fire a run at once).
SCHEDULE_KEYS = ("frequency", "time", "weekday", "day_of_month", "timezone")
_BOOL_KEYS = ("enabled", "clean_invalid", "clean_hard_bounces")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Module-level so tests can swap in fresh ones (an asyncio.Lock binds to the
# first event loop that contends it; pytest-asyncio gives each test a new loop).
_STATE_LOCK = asyncio.Lock()
_RESULTS_LOCK = asyncio.Lock()


class JobBusy(Exception):
    """A scan / cleanup is already running."""


# ── Time ────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    """Current UTC time. The seam tests patch instead of sleeping."""
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


# ── Settings state ──────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {
        # ── user settings ──
        "enabled": False,
        "frequency": "weekly",
        "time": "03:00",
        "weekday": 6,              # Monday=0 … Sunday=6 (Python's weekday())
        "day_of_month": 1,         # 1-28 so every month has the day
        "timezone": DEFAULT_TIMEZONE,
        "clean_invalid": True,
        "clean_hard_bounces": True,
        # ── runtime state ──
        "last_slot": None,         # ISO of the schedule slot already handled
        "last_scan": None,         # summary of the latest scan or cleanup
        "last_run": None,          # summary of the latest cleanup
        "recent_cleaned": [],      # audit trail of blocklisted addresses
    }


def _int_in(value, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return default
    return value


def _valid_iso(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        datetime.fromisoformat(value)
        return value
    except ValueError:
        return None


def load_state() -> dict:
    """Load email_cleanup.json, repairing anything a hand edit or crash broke.

    Never raises: it runs on every scheduler poll and on every Settings load.
    """
    state = _default_state()
    try:
        with open(CLEANUP_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return state
    except Exception as e:
        logger.error(f"Failed to load email cleanup state: {e}")
        return state
    if not isinstance(data, dict):
        return state

    for key in _BOOL_KEYS:
        if isinstance(data.get(key), bool):
            state[key] = data[key]
    if data.get("frequency") in FREQUENCIES:
        state["frequency"] = data["frequency"]
    if isinstance(data.get("time"), str) and _TIME_RE.match(data["time"]):
        state["time"] = data["time"]
    state["weekday"] = _int_in(data.get("weekday"), state["weekday"], 0, 6)
    state["day_of_month"] = _int_in(data.get("day_of_month"), state["day_of_month"], 1, 28)
    tz = data.get("timezone")
    if isinstance(tz, str):
        try:
            ZoneInfo(tz)
            state["timezone"] = tz
        except Exception:
            pass
    state["last_slot"] = _valid_iso(data.get("last_slot"))
    for key in ("last_scan", "last_run"):
        if isinstance(data.get(key), dict):
            state[key] = data[key]
    cleaned = data.get("recent_cleaned")
    if isinstance(cleaned, list):
        state["recent_cleaned"] = [c for c in cleaned if isinstance(c, dict)][:RECENT_CLEANED_LIMIT]
    return state


def _atomic_write_json(path, payload) -> None:
    """Write ``payload`` as JSON atomically (tempfile + fsync + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=path.stem + "_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except OSError as e:
        logger.error(f"Failed to save {path}: {e}")
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def save_state(state: dict) -> None:
    _atomic_write_json(CLEANUP_FILE, state)


def validate_patch(data) -> dict:
    """Validate a client settings patch (raises ``ValueError`` → router 400).

    Never coerces: ``"true"`` is not a boolean and ``true`` is not a day.
    """
    if not isinstance(data, dict) or not data:
        raise ValueError("Body must be a non-empty object")
    clean: dict = {}
    for key, value in data.items():
        if key not in SETTING_KEYS:
            raise ValueError(f"Unknown cleanup setting '{key}'")
        if key in _BOOL_KEYS:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
        elif key == "frequency":
            if value not in FREQUENCIES:
                raise ValueError("frequency must be daily, weekly or monthly")
        elif key == "time":
            if not isinstance(value, str) or not _TIME_RE.match(value):
                raise ValueError("time must look like HH:MM (24-hour)")
        elif key == "weekday":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6:
                raise ValueError("weekday must be a whole number from 0 (Monday) to 6 (Sunday)")
        elif key == "day_of_month":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 28:
                raise ValueError("day_of_month must be a whole number from 1 to 28")
        elif key == "timezone":
            if not isinstance(value, str):
                raise ValueError("timezone must be a string")
            try:
                ZoneInfo(value)
            except Exception:
                raise ValueError(f"Invalid timezone: {value}")
        clean[key] = value
    return clean


async def update_settings(patch: dict, now: Optional[datetime] = None) -> dict:
    """Merge a validated patch into the persisted state and return it.

    Raises ``ValueError`` for an invalid combination and ``OSError`` when the
    file cannot be written.
    """
    now = now or _now()
    async with _STATE_LOCK:
        state = load_state()
        before = {k: state[k] for k in SCHEDULE_KEYS}
        was_enabled = state["enabled"]
        state.update(patch)
        if state["enabled"] and not (state["clean_invalid"] or state["clean_hard_bounces"]):
            raise ValueError("Choose at least one kind of address to clean")
        schedule_moved = any(state[k] != before[k] for k in SCHEDULE_KEYS)
        if state["enabled"] and (not was_enabled or schedule_moved):
            # Baseline: the slot that is already in the past must not fire.
            state["last_slot"] = _iso(latest_slot(state, now))
        await asyncio.to_thread(save_state, state)
    return state


# ── Schedule maths ──────────────────────────────────────────────────────────

def _slot_at(cfg: dict, day: date) -> datetime:
    hh, mm = (int(p) for p in cfg["time"].split(":"))
    return datetime.combine(day, dtime(hh, mm), tzinfo=ZoneInfo(cfg["timezone"]))


def _add_month(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, day.day)


def _sub_month(day: date) -> date:
    return date(day.year - (day.month == 1), (day.month - 2) % 12 + 1, day.day)


def latest_slot(cfg: dict, now: datetime) -> datetime:
    """The most recent scheduled run time at or before ``now`` (aware)."""
    tz = ZoneInfo(cfg["timezone"])
    local = now.astimezone(tz)
    today = local.date()
    if cfg["frequency"] == "daily":
        day = today
        step_back = lambda d: d - timedelta(days=1)
    elif cfg["frequency"] == "weekly":
        day = today - timedelta(days=(today.weekday() - cfg["weekday"]) % 7)
        step_back = lambda d: d - timedelta(days=7)
    else:
        day = date(today.year, today.month, cfg["day_of_month"])
        step_back = _sub_month
    slot = _slot_at(cfg, day)
    if slot > local:
        slot = _slot_at(cfg, step_back(day))
    return slot


def next_slot(cfg: dict, now: datetime) -> datetime:
    """The first scheduled run time after ``now`` (aware)."""
    last = latest_slot(cfg, now)
    day = last.date()
    if cfg["frequency"] == "daily":
        day += timedelta(days=1)
    elif cfg["frequency"] == "weekly":
        day += timedelta(days=7)
    else:
        day = _add_month(day)
    return _slot_at(cfg, day)


def is_due(state: dict, now: datetime) -> bool:
    """A scheduled run is owed: enabled and a slot has passed unhandled."""
    if not state["enabled"]:
        return False
    slot = latest_slot(state, now)
    done = state["last_slot"]
    return done is None or datetime.fromisoformat(done) < slot


# ── Results store (the problem addresses) ───────────────────────────────────

def load_results() -> dict:
    """``{email: item}`` of the stored problem addresses. Never raises."""
    try:
        with open(RESULTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Failed to load email validation results: {e}")
        return {}
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return {}
    return {
        e: it for e, it in items.items()
        if isinstance(it, dict) and isinstance(it.get("subscriber_id"), int)
        and not isinstance(it.get("subscriber_id"), bool)
    }


async def _save_results(items: dict) -> None:
    """Persist ``items`` (caller holds ``_RESULTS_LOCK``), capped by severity."""
    if len(items) > RESULTS_CAP:
        rank = {"invalid": 0, "risky": 1, "unknown": 2}
        keep = sorted(items.items(), key=lambda kv: rank.get(kv[1].get("verdict"), 3))
        items = dict(keep[:RESULTS_CAP])
    await asyncio.to_thread(
        _atomic_write_json, RESULTS_FILE, {"updated_at": _iso(_now()), "items": items},
    )


def result_counts(items: Optional[dict] = None) -> dict:
    items = load_results() if items is None else items
    counts = {"all": len(items), "invalid": 0, "risky": 0, "unknown": 0}
    for it in items.values():
        v = it.get("verdict")
        if v in counts:
            counts[v] += 1
    return counts


def _matches(item: dict, verdict: str, needle: str) -> bool:
    if verdict and item.get("verdict") != verdict:
        return False
    if needle:
        return needle in item.get("email", "").lower() or needle in (item.get("name") or "").lower()
    return True


def query_results(verdict: str = "", search: str = "") -> list[dict]:
    """Stored problems filtered by verdict / free text, worst first."""
    needle = (search or "").strip().lower()
    rank = {"invalid": 0, "risky": 1, "unknown": 2}
    rows = [it for it in load_results().values() if _matches(it, verdict, needle)]
    rows.sort(key=lambda it: (rank.get(it.get("verdict"), 3), it.get("email", "")))
    return rows


async def dismiss_results(ids: list[int]) -> int:
    """Forget the stored problems of these subscribers (ListMonk untouched)."""
    wanted = set(ids)
    async with _RESULTS_LOCK:
        items = load_results()
        kept = {e: it for e, it in items.items() if it["subscriber_id"] not in wanted}
        removed = len(items) - len(kept)
        if removed:
            await _save_results(kept)
    return removed


async def blocklist_results(client, ids: list[int]) -> dict:
    """Blocklist the selected problem addresses.

    Only subscribers that are in the stored results can be blocklisted through
    here, so this endpoint cannot be used as a generic blocklist-by-id.
    Returns ``{blocklisted, errors, failed, skipped}``; partial failures are
    reported per id, never swallowed.
    """
    async with _RESULTS_LOCK:
        known = {it["subscriber_id"] for it in load_results().values()}
    todo = [i for i in ids if i in known]
    skipped = len(ids) - len(todo)
    done, failed = await _blocklist_chunks(client, todo)
    if done:
        await dismiss_results(done)
    return {"blocklisted": len(done), "errors": len(failed), "failed": failed,
            "skipped": skipped}


async def _blocklist_chunks(client, ids: list[int]) -> tuple[list[int], list[dict]]:
    """Blocklist ``ids`` in chunks. Returns ``(done_ids, failed[{id, error}])``."""
    done: list[int] = []
    failed: list[dict] = []
    for i in range(0, len(ids), CLEAN_CHUNK):
        chunk = ids[i:i + CLEAN_CHUNK]
        try:
            await client.blocklist_subscribers(chunk)
            done.extend(chunk)
            # Deliberate (a results action or the cleanup): auto-unblock must
            # not re-enable these six hours later.
            blocklist_registry.mark(chunk)
        except Exception as e:
            logger.error(f"Blocklisting {len(chunk)} subscribers failed: {e}")
            failed.extend({"id": sid, "error": str(e) or type(e).__name__} for sid in chunk)
    return done, failed


# ── Job (progress is in memory; results are persisted at the end) ───────────

def _idle_job() -> dict:
    return {
        "running": False, "kind": None, "phase": "idle", "list_id": None,
        "started_at": None, "checked": 0, "total": 0,
        "valid": 0, "invalid": 0, "risky": 0, "unknown": 0,
        "cancel_requested": False,
    }


_job: dict = _idle_job()


def reset_job() -> None:
    """Forget any job state (tests; a stuck flag after a crashed run)."""
    global _job
    _job = _idle_job()


def job_snapshot() -> dict:
    return dict(_job)


def request_cancel() -> bool:
    """Ask the running job to stop after the current page."""
    if _job["running"]:
        _job["cancel_requested"] = True
        return True
    return False


def _begin_job(kind: str, list_id: Optional[int]) -> None:
    """Claim the single job slot. Synchronous, so check-and-set is atomic."""
    if _job["running"]:
        raise JobBusy("A scan or cleanup is already running")
    _job.update(_idle_job())
    _job.update(running=True, kind=kind, list_id=list_id, phase="starting",
                started_at=_iso(_now()))


def start_job(client, kind: str, list_id: Optional[int] = None) -> None:
    """Start a background ``scan`` or ``cleanup`` (raises ``JobBusy``)."""
    _begin_job(kind, list_id)
    spawn(_execute(client, kind, list_id))


async def run_job(client, kind: str, list_id: Optional[int] = None) -> dict:
    """Run a job to completion in the caller's task (the scheduler's path)."""
    _begin_job(kind, list_id)
    return await _execute(client, kind, list_id)


def _safe_int(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def _collect_hard_bounced(client) -> tuple[set[int], set[str]]:
    """Subscriber ids / emails with a hard bounce, openers excluded.

    Same filtering as the Bounces page and the campaign hard-bounce counts: a
    subscriber who opened the campaign cannot have genuinely bounced it.
    """
    ids: set[int] = set()
    emails: set[str] = set()

    async def fold(chunk: list[dict]) -> None:
        for b in await filter_bounces_excluding_openers(client, chunk):
            sid = _safe_int(b.get("subscriber_id"))
            if sid is not None:
                ids.add(sid)
            email = ev.normalise(b.get("email") or "")
            if email:
                emails.add(email)

    chunk: list[dict] = []
    async for bounce in client.iter_pages(
        client.get_bounces, per_page=BOUNCE_PAGE_SIZE, bounce_type="hard",
    ):
        if bounce.get("type") != "hard":
            continue
        chunk.append(bounce)
        if len(chunk) >= BOUNCE_PAGE_SIZE:
            await fold(chunk)
            chunk = []
            if _job["cancel_requested"]:
                return ids, emails
    if chunk:
        await fold(chunk)
    return ids, emails


def _is_candidate(item: dict, cfg: dict) -> bool:
    """Should the cleanup blocklist this problem? Definitive findings only."""
    if item["verdict"] != "invalid":
        return False
    reasons = set(item["reasons"])
    if cfg["clean_invalid"] and reasons & ev.ADDRESS_INVALID_REASONS:
        return True
    return bool(cfg["clean_hard_bounces"] and ev.REASON_HARD_BOUNCE in reasons)


# Findings that rest on one DNS answer at one moment.
DNS_INVALID_REASONS = frozenset({ev.REASON_NO_DOMAIN, ev.REASON_NO_MX, ev.REASON_NULL_MX})


def _is_confirmed(item: dict, cfg: dict, dns_dead_before: set[int]) -> bool:
    """May a candidate be blocklisted in THIS run?

    A malformed address and a hard bounce are facts. "The domain does not
    exist / has no mail server" is one DNS answer: a domain in a registrar
    hold or with a botched zone for a day answers the same, and a blocklisting
    is permanent. So a DNS-only finding has to be seen by two scans (it is
    already in the stored results as DNS-dead) before the cleanup acts on it.
    """
    reasons = set(item["reasons"])
    if cfg["clean_invalid"] and ev.REASON_SYNTAX in reasons:
        return True
    if cfg["clean_hard_bounces"] and ev.REASON_HARD_BOUNCE in reasons:
        return True
    return item["subscriber_id"] in dns_dead_before


def _summary(kind: str, status: str, message: str, started: datetime,
             list_id: Optional[int]) -> dict:
    finished = _now()
    return {
        "kind": kind, "status": status, "message": message, "list_id": list_id,
        "started_at": _iso(started), "at": _iso(finished),
        "duration_seconds": round((finished - started).total_seconds(), 1),
        "checked": _job["checked"], "valid": _job["valid"],
        "invalid": _job["invalid"], "risky": _job["risky"], "unknown": _job["unknown"],
        "candidates": 0, "blocklisted": 0, "failed_blocklist": 0,
        "pending_confirmation": 0,
    }


async def _execute(client, kind: str, list_id: Optional[int]) -> dict:
    """The job body. Always releases the job slot and records a summary."""
    started = _now()
    summary = _summary(kind, "failed", "", started, list_id)
    cleaned: list[dict] = []
    try:
        summary = await _run(client, kind, list_id, started, cleaned)
    except Exception as e:
        logger.exception("Email %s failed", kind)
        summary = _summary(kind, "failed", f"{type(e).__name__}: {e}", started, list_id)
    finally:
        _job["running"] = False
        _job["phase"] = "done"
    try:
        await _record(summary, cleaned)
    except Exception:
        logger.exception("Could not record the email %s summary", kind)
    return summary


async def _run(client, kind: str, list_id: Optional[int], started: datetime,
               cleaned: list[dict]) -> dict:
    cfg = load_state()

    _job["phase"] = "dns check"
    if not await ev.resolver_healthy():
        return _summary(
            kind, "failed",
            "DNS lookups are not working from this server, so nothing was checked "
            "(no address was changed).", started, list_id)

    _job["phase"] = "bounces"
    hard_ids, hard_emails = await _collect_hard_bounced(client)

    _job["phase"] = "checking"
    async with _RESULTS_LOCK:
        stored_before = load_results()
    known_ids = {it["subscriber_id"] for it in stored_before.values()}
    # Subscribers an EARLIER scan already found DNS-dead (see _is_confirmed).
    dns_dead_before = {
        it["subscriber_id"] for it in stored_before.values()
        if it.get("verdict") == "invalid" and set(it.get("reasons") or []) & DNS_INVALID_REASONS
    }
    rechecked: set[int] = set()
    flagged: dict[str, dict] = {}
    completed = False

    page = 1
    while not _job["cancel_requested"]:
        result = await client.get_subscribers(
            page=page, per_page=PAGE_SIZE,
            query="subscribers.status = 'enabled'", list_id=list_id,
        )
        data = result.get("data") or {}
        rows = data.get("results") or []
        total = data.get("total")
        if isinstance(total, int) and not isinstance(total, bool):
            _job["total"] = total
        if not rows:
            completed = True
            break

        verdicts = await ev.validate_many([r.get("email") or "" for r in rows])
        now_iso = _iso(_now())
        for row, res in zip(rows, verdicts):
            sid = _safe_int(row.get("id"))
            if sid is None:
                continue
            if sid in known_ids:
                rechecked.add(sid)
            if sid in hard_ids or res["email"] in hard_emails:
                # A bounced address is dead whatever its DNS says.
                res["verdict"] = "invalid"
                res["reasons"] = [ev.REASON_HARD_BOUNCE,
                                  *[r for r in res["reasons"] if r != ev.REASON_HARD_BOUNCE]]
                res["detail"] = res["detail"] or "This address hard-bounced"
            _job["checked"] += 1
            _job[res["verdict"]] += 1
            if res["verdict"] != "valid":
                flagged[res["email"]] = {
                    "subscriber_id": sid,
                    "name": row.get("name") or "",
                    "email": res["email"],
                    "domain": res["domain"],
                    "verdict": res["verdict"],
                    "reasons": res["reasons"],
                    "suggestion": res["suggestion"],
                    "detail": res["detail"],
                    "checked_at": now_iso,
                }
        if isinstance(total, int) and page * PAGE_SIZE >= total:
            completed = True
            break
        page += 1

    cancelled = _job["cancel_requested"] and not completed

    # Persist what the scan found. A complete scan of everyone replaces the
    # store (anything not flagged is clean or gone); a partial or per-list scan
    # only replaces the entries of the subscribers it actually re-checked.
    async with _RESULTS_LOCK:
        current = load_results()
        if completed and list_id is None:
            merged = dict(flagged)
        else:
            merged = {e: it for e, it in current.items() if it["subscriber_id"] not in rechecked}
            merged.update(flagged)
        await _save_results(merged)

    summary = _summary(kind, "cancelled" if cancelled else "ok", "", started, list_id)
    if cancelled:
        summary["message"] = "Stopped before every subscriber was checked."
        return summary
    if kind != "cleanup":
        summary["message"] = f"Checked {summary['checked']} subscribers."
        return summary

    # ── cleanup: decide, hold or blocklist ──
    eligible = [it for it in flagged.values() if _is_candidate(it, cfg)]
    candidates = [it for it in eligible if _is_confirmed(it, cfg, dns_dead_before)]
    pending = len(eligible) - len(candidates)
    summary["candidates"] = len(candidates)
    summary["pending_confirmation"] = pending
    wait_note = (
        f" {pending} address(es) with a dead domain were seen for the first time; "
        "they are blocklisted if the next scan still finds them dead." if pending else "")
    # Judged on everything that looks dead, confirmed or not: a resolver or
    # data problem must raise the alarm on the first run, not the second.
    if (summary["checked"] >= HOLD_MIN_SAMPLE
            and len(eligible) / summary["checked"] > HOLD_FRACTION):
        summary["status"] = "held"
        summary["candidates"] = len(eligible)
        summary["message"] = (
            f"{len(eligible)} of {summary['checked']} subscribers "
            f"({len(eligible) * 100 // summary['checked']}%) look dead, which is more than "
            f"{int(HOLD_FRACTION * 100)}%. Nothing was blocklisted: review them on the "
            "Email Validation page.")
        return summary

    if not candidates:
        summary["message"] = "Nothing to clean." + wait_note
        return summary

    _job["phase"] = "blocklisting"
    by_id = {it["subscriber_id"]: it for it in candidates}
    done, failed = await _blocklist_chunks(client, list(by_id))
    summary["blocklisted"] = len(done)
    summary["failed_blocklist"] = len(failed)
    stamp = _iso(_now())
    cleaned.extend({"at": stamp, "subscriber_id": sid, "email": by_id[sid]["email"],
                    "reasons": by_id[sid]["reasons"]} for sid in done)
    if done:
        await dismiss_results(done)
    if failed:
        summary["status"] = "failed"
        summary["message"] = (f"Blocklisted {len(done)}; {len(failed)} could not be "
                              f"blocklisted ({failed[0]['error']}).")
    else:
        summary["message"] = f"Blocklisted {len(done)} dead addresses." + wait_note
    return summary


async def _record(summary: dict, cleaned: list[dict]) -> None:
    async with _STATE_LOCK:
        state = load_state()
        state["last_scan"] = summary
        if summary["kind"] == "cleanup":
            state["last_run"] = summary
            if cleaned:
                state["recent_cleaned"] = (cleaned + state["recent_cleaned"])[:RECENT_CLEANED_LIMIT]
        await asyncio.to_thread(save_state, state)


# ── Scheduler ───────────────────────────────────────────────────────────────

async def tick(client, now: Optional[datetime] = None) -> Optional[dict]:
    """Run the scheduled cleanup if one is owed. Called by the 60 s loop."""
    now = now or _now()
    state = load_state()
    if _job["running"] or not is_due(state, now):
        return None
    last = state["last_run"]
    if last and last.get("status") == "failed" and last.get("at"):
        try:
            if now - datetime.fromisoformat(last["at"]) < RETRY_AFTER_FAILURE:
                return None
        except ValueError:
            pass

    try:
        summary = await run_job(client, "cleanup")
    except JobBusy:
        return None
    if summary["status"] != "failed":
        # Handled (ok / held / cancelled): the slot is used up. A failed run
        # keeps the slot open and is retried after RETRY_AFTER_FAILURE.
        async with _STATE_LOCK:
            fresh = load_state()
            fresh["last_slot"] = _iso(latest_slot(fresh, now))
            await asyncio.to_thread(save_state, fresh)
    return summary


# ── API views ───────────────────────────────────────────────────────────────

def view(state: Optional[dict] = None, now: Optional[datetime] = None) -> dict:
    """The settings + schedule + last runs payload for the Settings tab."""
    state = state or load_state()
    now = now or _now()
    out = {k: state[k] for k in SETTING_KEYS}
    out["next_run"] = _iso(next_slot(state, now)) if state["enabled"] else None
    out["last_scan"] = state["last_scan"]
    out["last_run"] = state["last_run"]
    out["recent_cleaned"] = state["recent_cleaned"]
    out["job"] = job_snapshot()
    out["hold_percent"] = int(HOLD_FRACTION * 100)
    return out
