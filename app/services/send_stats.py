"""
Daily send statistics: how many campaign emails went out on each day.

ListMonk only stores a cumulative ``sent`` per campaign, never *when* each
message left, so "sent today" cannot be read from it after the fact. This
module counts sends as they happen, always (warm-up on or off), with the same
delta method as the warm-up quota (``warmup.count_deltas``):

* the warm-up loop polls ``/api/campaigns/running/stats`` every 1-3 s and
  passes the snapshot to ``observe()``;
* every increase of a campaign's ``sent`` since the previous poll is added to
  today's bucket, per campaign;
* the first poll only *baselines* the counters, so a campaign's earlier
  history is never charged to the day counting started.

Days are calendar days in the timezone the caller passes (the warm-up
timezone while warm-up is on, so both pages agree, otherwise the send-window
timezone). History therefore starts the day this module first ran; on first
run it is seeded from the warm-up history (days that warm-up already counted).
"""

import asyncio
import json
import logging
import os
import tempfile
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import settings
from app.services import warmup

logger = logging.getLogger("send_stats")

SEND_STATS_FILE = settings.data_path("send_stats.json")

# Days kept in the file (a little over a year: enough for any chart range).
DAYS_LIMIT = 400
# Per-campaign entries kept for one day (a day never has more in practice).
CAMPAIGNS_PER_DAY_LIMIT = 500
DEFAULT_TIMEZONE = "UTC"

# Serialises load → mutate → save of send_stats.json (reset in conftest).
_STATS_LOCK = asyncio.Lock()


def _default_state() -> dict:
    return {
        "timezone": DEFAULT_TIMEZONE,  # timezone of the most recent poll
        "tracking_since": None,        # first day with any count (ISO date)
        "counting_since": None,        # first day this module counted itself
        "seen": {},                    # campaign id (str) -> last cumulative `sent`
        "running": [],                 # campaign ids running at the last poll
        "unsettled": [],               # vanished campaigns whose final count is pending
        "baselined": False,
        "days": {},                    # ISO date -> {"total": n, "campaigns": {id: n}}
    }


# ── State file ──────────────────────────────────────────────────────────────

def _nonneg_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_date(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _clean_day(raw) -> Optional[dict]:
    if not isinstance(raw, dict) or not _nonneg_int(raw.get("total")):
        return None
    campaigns = raw.get("campaigns")
    clean = {
        str(k): v for k, v in (campaigns.items() if isinstance(campaigns, dict) else [])
        if str(k).isdigit() and _nonneg_int(v)
    }
    day = {"total": raw["total"], "campaigns": clean}
    if raw.get("source") == "warmup":
        day["source"] = "warmup"
    return day


def load_state() -> dict:
    """Load send_stats.json; any corrupt field falls back to its default."""
    state = _default_state()
    try:
        with open(SEND_STATS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return state
    except Exception as e:
        logger.error(f"Failed to load send statistics: {e}")
        return state
    if not isinstance(data, dict):
        return state

    tz = data.get("timezone")
    if isinstance(tz, str):
        try:
            ZoneInfo(tz)
            state["timezone"] = tz
        except Exception:
            pass
    state["tracking_since"] = _valid_date(data.get("tracking_since"))
    state["counting_since"] = _valid_date(data.get("counting_since"))
    state["baselined"] = data.get("baselined") is True
    seen = data.get("seen")
    if isinstance(seen, dict):
        state["seen"] = {str(k): v for k, v in seen.items() if _nonneg_int(v)}
    for key in ("running", "unsettled"):
        ids = data.get(key)
        if isinstance(ids, list):
            state[key] = [i for i in ids if isinstance(i, int) and not isinstance(i, bool)]
    days = data.get("days")
    if isinstance(days, dict):
        for d, raw in days.items():
            key, day = _valid_date(d), _clean_day(raw)
            if key and day is not None:
                state["days"][key] = day
    return state


def save_state(state: dict) -> None:
    """Write send_stats.json atomically (tempfile + fsync + replace)."""
    temp_path = None
    try:
        SEND_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            dir=SEND_STATS_FILE.parent, prefix="send_stats_", suffix=".tmp",
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, SEND_STATS_FILE)
        temp_path = None
    except OSError as e:
        logger.error(f"Failed to save send statistics to {SEND_STATS_FILE}: {e}")
        raise
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _seed_from_warmup(state: dict) -> None:
    """First run only: adopt the days the warm-up counter already recorded."""
    try:
        warm = warmup.load_state()
    except Exception:
        return
    for entry in warm.get("history") or []:
        d = _valid_date(entry.get("date"))
        sent = entry.get("sent")
        if d and _nonneg_int(sent):
            state["days"][d] = {"total": sent, "campaigns": {}, "source": "warmup"}
    d = _valid_date(warm.get("date"))
    if warm.get("enabled") and d and _nonneg_int(warm.get("sent_today")) and warm["sent_today"]:
        state["days"][d] = {"total": warm["sent_today"], "campaigns": {}, "source": "warmup"}
    if state["days"]:
        state["tracking_since"] = min(state["days"])


def _trim(state: dict) -> None:
    days = state["days"]
    if len(days) > DAYS_LIMIT:
        for d in sorted(days)[: len(days) - DAYS_LIMIT]:
            del days[d]


def today_in(tz_name: str, now: Optional[datetime] = None) -> str:
    tz = ZoneInfo(tz_name)
    current = now.astimezone(tz) if now is not None else datetime.now(tz)
    return current.date().isoformat()


# ── Counting ────────────────────────────────────────────────────────────────

async def observe(client, stats: list[dict], tz_name: str,
                  now: Optional[datetime] = None) -> int:
    """Add the sends since the previous poll to today's bucket; returns them.

    ``stats`` is the running-stats snapshot the warm-up loop already fetched.
    Like ``warmup.observe``, the network calls (baseline, final counts of
    campaigns that stopped) happen outside the lock and the write is a fresh
    load → mutate → save inside it.
    """
    try:
        ZoneInfo(tz_name)
    except Exception:
        tz_name = DEFAULT_TIMEZONE
    first = load_state()
    running_ids = [s["id"] for s in stats]

    baseline = None
    if not first["baselined"]:
        baseline = await warmup._fetch_baseline(client, stats)

    finals: dict = {}
    retry: list[int] = []
    for cid in (set(first["running"]) | set(first["unsettled"])) - set(running_ids):
        try:
            finals[cid] = await warmup._fetch_final(client, cid)
        except Exception as e:
            logger.warning(f"Send stats could not read the final count of campaign #{cid}: {e}")
            retry.append(cid)

    async with _STATS_LOCK:
        fresh_file = not SEND_STATS_FILE.exists()
        state = load_state()
        before = json.dumps(state, sort_keys=True)
        if fresh_file:
            _seed_from_warmup(state)
        state["timezone"] = tz_name
        today = today_in(tz_name, now)
        counted = 0
        if not state["baselined"]:
            if baseline is not None:
                state["seen"] = {str(cid): sent for cid, sent in baseline.items()}
                state["baselined"] = True
                state["counting_since"] = today
                if state["tracking_since"] is None or state["tracking_since"] > today:
                    state["tracking_since"] = today
        else:
            if state["counting_since"] is None:
                # Baselined by a version without this field: count from now.
                state["counting_since"] = today
            deltas = warmup.count_deltas(state["seen"], stats, finals)
            counted = sum(deltas.values())
            if counted:
                day = state["days"].setdefault(today, {"total": 0, "campaigns": {}})
                day["total"] += counted
                camps = day["campaigns"]
                for key, n in deltas.items():
                    if key in camps or len(camps) < CAMPAIGNS_PER_DAY_LIMIT:
                        camps[key] = camps.get(key, 0) + n
        state["running"] = running_ids
        state["unsettled"] = retry
        _trim(state)
        # Most polls send nothing: skip the rewrite + fsync then.
        if fresh_file or json.dumps(state, sort_keys=True) != before:
            save_state(state)
    return counted


# ── Read-only view ──────────────────────────────────────────────────────────

def daily(days: int, now: Optional[datetime] = None) -> dict:
    """The last ``days`` calendar days (oldest first) plus today's breakdown.

    A day is ``tracked`` from ``counting_since`` on (this module watched it, so
    a missing entry really is zero). Before that, only the days seeded from the
    warm-up history are known: the gaps between them are reported with
    ``tracked: false`` — unknown, which is not the same as nothing sent.
    """
    state = load_state()
    tz_name = state["timezone"]
    today = date.fromisoformat(today_in(tz_name, now))
    since = state["tracking_since"]
    counting = state["counting_since"]
    out = []
    for offset in range(days - 1, -1, -1):
        d = (today - timedelta(days=offset)).isoformat()
        entry = state["days"].get(d)
        tracked = entry is not None or (counting is not None and d >= counting)
        out.append({"date": d, "total": entry["total"] if entry else 0, "tracked": tracked})
    today_entry = state["days"].get(today.isoformat()) or {"total": 0, "campaigns": {}}
    by_campaign = sorted(
        ({"id": int(k), "sent": v} for k, v in today_entry["campaigns"].items()),
        key=lambda c: c["sent"], reverse=True,
    )
    tracked_totals = [d["total"] for d in out if d["tracked"]]
    return {
        "timezone": tz_name,
        "today": today.isoformat(),
        "sent_today": today_entry["total"],
        "today_by_campaign": by_campaign,
        "tracking_since": since,
        "days": out,
        "period_total": sum(tracked_totals),
        "active": state["baselined"],
    }
