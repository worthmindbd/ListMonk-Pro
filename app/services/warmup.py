"""
Sending warm-up: raise the daily send volume step by step.

A fresh domain / mailbox / IP has no sending reputation, so a sudden burst of
mail looks like spam to the receiving providers. Warm-up caps how many campaign
emails go out per day and lifts the cap gradually (day 1 = 10, then +20 % a day
… up to the target volume).

How the cap is enforced
-----------------------
ListMonk's own sliding-window rate limit is not usable for this: it is global,
held in memory, anchored to the process start time and only changes after a
settings reload — which ListMonk refuses to perform while a campaign is
running. So the cap lives here:

* ``observe()`` polls ``/api/campaigns/running/stats`` (live, in-memory ``sent``
  counts) and adds every increase to today's counter.
* ``campaign_scheduler.enforce_warmup`` pauses the running campaigns the moment
  the counter reaches today's quota, and the scheduler tick resumes them once a
  new day starts (``has_quota``). Pausing is immediate on ListMonk's side and
  queued messages of a paused campaign are discarded rather than sent.

The counter is delta-based (``seen`` holds the last cumulative ``sent`` per
campaign), so messages sent while this app was restarting are still counted on
the next poll, and a campaign's history from before warm-up was switched on is
never charged to it.

The ramp only moves forward after a day that actually used (at least)
``ADVANCE_MIN_FRACTION`` of its quota. A calendar-driven ramp would let a
weekend, an empty queue or a long pause jump you to a high volume on a cold
domain, which is exactly what warm-up exists to prevent.
"""

import asyncio
import json
import logging
import math
import os
import tempfile
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from app.config import settings

logger = logging.getLogger("warmup")

WARMUP_FILE = settings.data_path("warmup.json")

# The ramp is computed, never stored; this bounds the computation and the
# payload. 365 steps is far beyond any realistic warm-up.
MAX_RAMP_DAYS = 365
# A day only "counts" (and unlocks the next step) when at least this share of
# its quota was really sent.
ADVANCE_MIN_FRACTION = 0.8
HISTORY_LIMIT = 60
# Upper bound for the per-campaign cumulative counters kept between polls.
SEEN_LIMIT = 1000
# Longest forecast ``estimate_days`` will compute (ten years of daily steps).
MAX_FORECAST_DAYS = 3650

# Starting points offered by the UI. The numbers are conservative rules of
# thumb for the *shape* of a ramp; the target volume is always the user's own.
RAMP_PRESETS = {
    "gentle": {
        "label": "Gentle",
        "start_volume": 10,
        "growth_percent": 10,
        "summary": "+10 % a day. New domain or mailbox, cold outreach.",
    },
    "standard": {
        "label": "Standard",
        "start_volume": 10,
        "growth_percent": 20,
        "summary": "+20 % a day. Good default for most senders.",
    },
    "doubling": {
        "label": "Doubling",
        "start_volume": 10,
        "growth_percent": 100,
        "summary": "×2 a day. Only for an established domain and an engaged list.",
    },
}

# Settings the API may change, and the numeric range of each.
SETTING_KEYS = ("enabled", "timezone", "start_volume", "growth_percent", "target_volume")
PATCH_KEYS = SETTING_KEYS + ("level",)
_INT_RANGES = {
    "start_volume": (1, 10_000),
    "growth_percent": (0, 300),
    "target_volume": (1, 100_000),
    "level": (1, MAX_RAMP_DAYS),
}

DEFAULT_TIMEZONE = "UTC"

# Serialises every load → mutate → save of warmup.json. Module level so tests
# can swap in a fresh lock (a contended asyncio.Lock binds to the first event
# loop that used it, and pytest-asyncio gives each test a new loop).
_WARMUP_LOCK = asyncio.Lock()


def _default_state() -> dict:
    return {
        # ── user settings ──
        "enabled": False,
        "timezone": DEFAULT_TIMEZONE,
        "start_volume": 10,
        "growth_percent": 20,
        "target_volume": 200,
        # ── runtime state (owned by observe / the day roll-over) ──
        "level": 1,            # 1-based ramp step we are on
        "date": None,          # day (in `timezone`) that sent_today belongs to
        "sent_today": 0,
        "seen": {},            # campaign id (str) -> last cumulative `sent`
        "running": [],         # campaign ids seen running at the last poll
        "unsettled": [],       # vanished campaigns whose final count is still to be read
        "baselined": False,    # `seen` was snapshotted since warm-up was enabled
        "history": [],         # finished days, oldest first
    }


# ── State file ──────────────────────────────────────────────────────────────

def _int_in(value, default: int, low: int, high: int) -> int:
    """``value`` when it is an int inside the range, else ``default``."""
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return default
    return value


def _valid_date(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def load_state() -> dict:
    """Load warmup.json, repairing anything a hand edit or crash corrupted.

    Every field is validated individually: a bad value falls back to its
    default instead of raising, because this runs on every poll and on every
    scheduler tick — an exception here would silently stop quota enforcement.
    """
    state = _default_state()
    try:
        with open(WARMUP_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return state
    except Exception as e:
        logger.error(f"Failed to load warm-up state: {e}")
        return state
    if not isinstance(data, dict):
        return state

    if isinstance(data.get("enabled"), bool):
        state["enabled"] = data["enabled"]
    tz = data.get("timezone")
    if isinstance(tz, str):
        try:
            ZoneInfo(tz)
            state["timezone"] = tz
        except Exception:
            pass
    for key in ("start_volume", "growth_percent", "target_volume", "level"):
        low, high = _INT_RANGES[key]
        state[key] = _int_in(data.get(key), state[key], low, high)
    if state["target_volume"] < state["start_volume"]:
        state["target_volume"] = state["start_volume"]
    state["sent_today"] = _int_in(data.get("sent_today"), 0, 0, 10**9)
    state["date"] = _valid_date(data.get("date"))
    state["baselined"] = data.get("baselined") is True

    seen = data.get("seen")
    if isinstance(seen, dict):
        state["seen"] = {
            str(k): v for k, v in seen.items()
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0
        }
    for key in ("running", "unsettled"):
        ids = data.get(key)
        if isinstance(ids, list):
            state[key] = [i for i in ids if isinstance(i, int) and not isinstance(i, bool)]
    history = data.get("history")
    if isinstance(history, list):
        state["history"] = [h for h in history if isinstance(h, dict)][-HISTORY_LIMIT:]
    return state


def save_state(state: dict) -> None:
    """Write warmup.json atomically (tempfile + fsync + replace)."""
    temp_path = None
    try:
        WARMUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            dir=WARMUP_FILE.parent, prefix="warmup_", suffix=".tmp",
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, WARMUP_FILE)
        temp_path = None
    except OSError as e:
        logger.error(f"Failed to save warm-up state to {WARMUP_FILE}: {e}")
        raise
    finally:
        # A failed write must not leave its temp file behind: this runs on
        # every poll, so a full disk used to gain one stray file per second.
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


# ── Ramp maths ──────────────────────────────────────────────────────────────

def build_ramp(start: int, growth_percent: int, target: int) -> list[int]:
    """Daily volumes from day 1 until the target is reached.

    Each day grows by ``growth_percent`` (integer maths, rounded half up) but by
    at least one email, so a small start with a small percentage still climbs
    instead of stalling on rounding. The last entry is exactly ``target``.
    ``100`` gives 10, 20, 40, 80 …
    """
    target = max(1, target)
    volume = min(max(1, start), target)
    volumes = [volume]
    while volume < target and len(volumes) < MAX_RAMP_DAYS:
        grown = (volume * (100 + growth_percent) + 50) // 100
        volume = min(target, max(volume + 1, grown))
        volumes.append(volume)
    return volumes


def ramp_for(state: dict) -> list[int]:
    return build_ramp(state["start_volume"], state["growth_percent"], state["target_volume"])


def _qualifies(sent: int, quota: int) -> bool:
    """Did a day use enough of its quota to unlock the next step?"""
    return sent >= max(1, math.ceil(quota * ADVANCE_MIN_FRACTION))


def _clamp_level(level: int, ramp: list[int]) -> int:
    return min(max(1, level), len(ramp))


# ── Time ────────────────────────────────────────────────────────────────────

def _now(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def today_for(state: dict, now: Optional[datetime] = None) -> str:
    """Today's date in the warm-up timezone (the day a quota belongs to)."""
    tz = ZoneInfo(state["timezone"])
    current = now.astimezone(tz) if now is not None else _now(state["timezone"])
    return current.date().isoformat()


# ── Read-only view ──────────────────────────────────────────────────────────

def view(state: dict, now: Optional[datetime] = None) -> dict:
    """Everything the UI and the scheduler need, derived without mutating.

    Between midnight and the next poll the stored counters still describe
    yesterday (``pending_rollover``): the view then reports the new day's
    quota with a zero counter, so a campaign is never held back by a stale
    "quota reached" for the few seconds before the roll-over is persisted.
    """
    today = today_for(state, now)
    ramp = ramp_for(state)
    level = _clamp_level(state["level"], ramp)
    pending = state["date"] is not None and state["date"] != today
    sent = state["sent_today"]
    if pending:
        if _qualifies(sent, ramp[level - 1]):
            level = min(level + 1, len(ramp))
        sent = 0
    quota = ramp[level - 1]
    remaining = max(0, quota - sent)

    if not state["enabled"]:
        label = "off"
    elif remaining <= 0:
        label = "quota_reached"
    elif state["running"]:
        label = "sending"
    else:
        label = "idle"

    return {
        "enabled": state["enabled"],
        "timezone": state["timezone"],
        "start_volume": state["start_volume"],
        "growth_percent": state["growth_percent"],
        "target_volume": state["target_volume"],
        "day": level,
        "quota": quota,
        "sent_today": sent,
        "remaining": remaining,
        "state": label,
        "today": today,
        "pending_rollover": pending,
        "next_quota": ramp[min(level + 1, len(ramp)) - 1],
        "at_target": level >= len(ramp),
        "days_to_target": len(ramp) - level,
        "ramp": ramp,
        "history": list(reversed(state["history"][-14:])),
        "advance_min_percent": int(ADVANCE_MIN_FRACTION * 100),
        "presets": RAMP_PRESETS,
    }


def has_quota(state: dict, now: Optional[datetime] = None) -> bool:
    """May campaigns send right now as far as warm-up is concerned?"""
    return view(state, now)["remaining"] > 0


def estimate_days(state: dict, backlog: int, now: Optional[datetime] = None) -> int:
    """Days *after today* needed to send ``backlog`` emails on this ramp.

    Assumes every day's quota is fully used, so it is a lower bound — a day
    that falls short holds the ramp back. ``0`` means today's remaining quota
    already covers it.
    """
    current = view(state, now)
    left = backlog - current["remaining"]
    ramp = current["ramp"]
    level = current["day"]
    days = 0
    while left > 0 and days < MAX_FORECAST_DAYS:
        level = min(level + 1, len(ramp))
        left -= ramp[level - 1]
        days += 1
    return days


# ── Settings ────────────────────────────────────────────────────────────────

def validate_patch(data) -> dict:
    """Validate a client patch (raises ValueError, routers answer 400).

    Never coerces: ``"20"`` is not a growth rate and ``true`` is not a volume,
    so a malformed body cannot write a surprising value.
    """
    if not isinstance(data, dict) or not data:
        raise ValueError("Body must be a non-empty object")
    clean: dict = {}
    for key, value in data.items():
        if key not in PATCH_KEYS:
            raise ValueError(f"Unknown warm-up setting '{key}'")
        if key == "enabled":
            if not isinstance(value, bool):
                raise ValueError("enabled must be true or false")
        elif key == "timezone":
            if not isinstance(value, str):
                raise ValueError("timezone must be a string")
            try:
                ZoneInfo(value)
            except Exception:
                raise ValueError(f"Invalid timezone: {value}")
        else:
            low, high = _INT_RANGES[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} must be a whole number")
            if not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
        clean[key] = value
    return clean


async def update_settings(patch: dict, now: Optional[datetime] = None) -> dict:
    """Merge a validated patch into the persisted state and return the state.

    Raises ValueError for a combination of values that is only invalid as a
    whole (target below the start volume) and OSError when the write fails.
    """
    async with _WARMUP_LOCK:
        state = load_state()
        was_enabled = state["enabled"]
        old_tz = state["timezone"]

        for key in SETTING_KEYS:
            if key in patch:
                state[key] = patch[key]
        if state["target_volume"] < state["start_volume"]:
            raise ValueError("target_volume must be at least start_volume")

        if "level" in patch:
            state["level"] = _clamp_level(patch["level"], ramp_for(state))
        else:
            state["level"] = _clamp_level(state["level"], ramp_for(state))

        # A new timezone moves "today" without a day having passed: keep the
        # counter on the new calendar date instead of closing a half-day and
        # advancing the ramp on it.
        if state["timezone"] != old_tz and state["date"] is not None:
            state["date"] = today_for(state, now)

        # Switching warm-up on or off changes what the counters mean (nothing
        # is tracked while it is off), so the per-campaign baseline is retaken.
        if state["enabled"] != was_enabled:
            state["baselined"] = False
            state["running"] = []
            state["unsettled"] = []
        if state["enabled"] and state["date"] is None:
            state["date"] = today_for(state, now)

        save_state(state)
        return state


# ── Day roll-over and counting ──────────────────────────────────────────────

def _roll_day(state: dict, now: Optional[datetime] = None) -> bool:
    """Close the stored day when the calendar date has moved on.

    Records the finished day, advances the ramp when it earned it, and resets
    the counter. Returns True when a new day started. Only one day is ever
    closed, however long the app was down: the days in between had no sending
    we know about, so they cannot unlock further steps.
    """
    today = today_for(state, now)
    if state["date"] == today:
        return False
    if state["date"] is not None:
        ramp = ramp_for(state)
        level = _clamp_level(state["level"], ramp)
        quota = ramp[level - 1]
        sent = state["sent_today"]
        advanced = _qualifies(sent, quota)
        state["history"].append({
            "date": state["date"], "day": level, "quota": quota,
            "sent": sent, "advanced": advanced,
        })
        del state["history"][:-HISTORY_LIMIT]
        state["level"] = min(level + 1, len(ramp)) if advanced else level
    state["date"] = today
    state["sent_today"] = 0
    return True


def _nonneg(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _apply_counts(state: dict, stats: list[dict], finals: dict) -> int:
    """Add the sends since the last poll to the counters; returns the delta.

    ``stats`` are the currently running campaigns; ``finals`` the last known
    counts of campaigns that left the running list (``None`` = deleted).
    A campaign never seen before starts from zero: it either is brand new, or
    its earlier sends happened while warm-up was off and were baselined away.
    """
    seen = state["seen"]
    counted = 0
    for stat in stats:
        key = str(stat["id"])
        sent = _nonneg(stat.get("sent"))
        prev = seen.get(key, 0)
        if sent > prev:
            counted += sent - prev
            seen[key] = sent
        else:
            seen.setdefault(key, prev)
    for cid, final in finals.items():
        key = str(cid)
        if final is None:
            seen.pop(key, None)
            continue
        sent, status = final
        prev = seen.get(key, 0)
        counted += max(0, sent - prev)
        if status in ("finished", "cancelled"):
            seen.pop(key, None)
        else:
            seen[key] = max(prev, sent)
    if len(seen) > SEEN_LIMIT:
        # Oldest campaigns have the lowest ids; they are the ones finished.
        for key in sorted(seen, key=int)[: len(seen) - SEEN_LIMIT]:
            del seen[key]
    return counted


async def _fetch_running(client) -> list[dict]:
    resp = await client.get_running_stats()
    data = resp.get("data") if isinstance(resp, dict) else None
    return [
        s for s in (data or [])
        if isinstance(s, dict) and isinstance(s.get("id"), int) and not isinstance(s.get("id"), bool)
    ]


async def _fetch_baseline(client, stats: list[dict]) -> dict[int, int]:
    """Cumulative ``sent`` of every running or paused campaign right now."""
    out: dict[int, int] = {}
    for status in ("running", "paused"):
        for camp in await client.paginate_all(client.get_campaigns, per_page=100, status=status):
            if isinstance(camp.get("id"), int):
                out[camp["id"]] = _nonneg(camp.get("sent"))
    # The live in-memory count is ahead of the database for a running campaign.
    for stat in stats:
        out[stat["id"]] = max(out.get(stat["id"], 0), _nonneg(stat.get("sent")))
    return out


async def _fetch_final(client, campaign_id: int):
    """``(sent, status)`` of a campaign, or None when it no longer exists."""
    try:
        data = (await client.get_campaign(campaign_id)).get("data") or {}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise
    return _nonneg(data.get("sent")), str(data.get("status") or "")


async def observe(client, now: Optional[datetime] = None) -> dict:
    """One poll: roll the day if needed and count what was sent since the last.

    Network calls happen outside the lock; the read-modify-write of
    warmup.json happens inside it, against a fresh load, so a settings change
    made while the request was in flight is never overwritten with stale state.
    """
    first = load_state()
    if not first["enabled"]:
        return {"enabled": False, "rolled": False, "running": [], "remaining": 0}

    stats = await _fetch_running(client)
    running_ids = [s["id"] for s in stats]

    baseline = None
    if not first["baselined"]:
        baseline = await _fetch_baseline(client, stats)

    finals: dict = {}
    retry: list[int] = []
    for cid in (set(first["running"]) | set(first["unsettled"])) - set(running_ids):
        try:
            finals[cid] = await _fetch_final(client, cid)
        except Exception as e:
            logger.warning(f"Warm-up could not read the final count of campaign #{cid}: {e}")
            retry.append(cid)

    async with _WARMUP_LOCK:
        state = load_state()
        if not state["enabled"]:
            return {"enabled": False, "rolled": False, "running": [], "remaining": 0}
        before = json.dumps(state, sort_keys=True)
        rolled = _roll_day(state, now)
        counted = 0
        if not state["baselined"]:
            if baseline is not None:
                state["seen"] = {str(cid): sent for cid, sent in baseline.items()}
                state["baselined"] = True
            # else: warm-up was switched on while this poll was in flight; the
            # next poll takes the baseline. Counting now would charge the
            # campaign's whole history to today.
        else:
            counted = _apply_counts(state, stats, finals)
            state["sent_today"] += counted
        state["running"] = running_ids
        state["unsettled"] = retry
        # Most polls change nothing (nothing running, nothing sent): skip the
        # rewrite + fsync instead of doing one every 1-3 seconds all day.
        if json.dumps(state, sort_keys=True) != before:
            save_state(state)
        current = view(state, now)

    return {
        "enabled": True,
        "rolled": rolled,
        "running": running_ids,
        "counted": counted,
        "quota": current["quota"],
        "sent_today": current["sent_today"],
        "remaining": current["remaining"],
    }
