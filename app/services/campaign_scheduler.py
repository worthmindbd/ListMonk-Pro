"""
Campaign Scheduler: Auto-pause/resume campaigns based on send time windows.

Checks every 60 seconds:
- If current time is OUTSIDE the send window and campaign is running -> pause it
- If current time is INSIDE the send window and campaign was auto-paused -> resume it

Tracks which campaigns were auto-paused so it doesn't interfere with manual pauses.
Disabling the scheduler resumes the campaigns it auto-paused.

Warm-up (app/services/warmup.py) is a second reason to hold campaigns back: a
campaign may only send while it is inside the send window (when that scheduler
is on) AND today's warm-up quota is not used up (when warm-up is on). Both
share the one ``auto_paused_campaigns`` list, so a campaign paused for either
reason resumes only when *neither* still applies. ``warmup_loop`` is the fast
(1-3 s) path that pauses campaigns the moment the quota is reached; the 60 s
tick below does the resuming.
"""

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import settings
from app.services import send_stats, warmup
from app.services.listmonk_client import ListMonkClient

logger = logging.getLogger("campaign_scheduler")

SCHEDULE_FILE = settings.data_path("schedule.json")

# Default schedule
DEFAULT_SCHEDULE = {
    "enabled": False,
    "timezone": "US/Eastern",
    "start_hour": 8,
    "start_minute": 0,
    "end_hour": 20,
    "end_minute": 0,
    "days": ["mon", "tue", "wed", "thu", "fri"],
    "auto_paused_campaigns": [],
}

# Serialises the whole load → mutate → save cycle on schedule.json. The 60s
# background loop, the PUT-triggered tick, and POST /api/scheduler/run can
# overlap; two concurrent read-modify-write cycles would clobber each other's
# changes (lost settings, campaigns that never auto-resume). Module level so
# tests can swap in a fresh lock (a contended asyncio.Lock binds itself to the
# event loop that first used it).
_SCHED_LOCK = asyncio.Lock()

# User-facing schedule keys the API may write. Runtime state
# (auto_paused_campaigns) belongs exclusively to the scheduler tick.
SCHEDULE_SETTING_KEYS = (
    "enabled", "timezone", "start_hour", "start_minute",
    "end_hour", "end_minute", "days",
)


def load_schedule() -> dict:
    """Load schedule from JSON file."""
    if SCHEDULE_FILE.exists():
        try:
            with open(SCHEDULE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # Explicit JSON nulls must not override the defaults: a
                # hand-edited or corrupted `"days": null` / `"timezone": null`
                # used to crash every scheduler tick (and GET /api/scheduler)
                # with a TypeError before the tick's own guards could run.
                merged = {
                    **DEFAULT_SCHEDULE,
                    **{k: v for k, v in data.items() if v is not None},
                }
                # List-valued keys must actually be lists.
                for list_key in ("days", "auto_paused_campaigns"):
                    if not isinstance(merged.get(list_key), list):
                        merged[list_key] = list(DEFAULT_SCHEDULE[list_key])
                # The timezone is parsed on every tick; validate it once here
                # so an invalid name falls back to the default immediately.
                try:
                    ZoneInfo(merged["timezone"])
                except Exception:
                    merged["timezone"] = DEFAULT_SCHEDULE["timezone"]
                # The window bounds become datetime.time on every tick (and in
                # GET /api/scheduler); a hand-edited "start_hour": 99 — or a
                # quoted "8" — would raise, 500-ing the Settings page and
                # failing every tick, silently disabling window enforcement.
                for key, maximum in (("start_hour", 23), ("end_hour", 23),
                                     ("start_minute", 59), ("end_minute", 59)):
                    value = merged.get(key)
                    if (isinstance(value, bool) or not isinstance(value, int)
                            or not 0 <= value <= maximum):
                        merged[key] = DEFAULT_SCHEDULE[key]
                return merged
        except Exception as e:
            logger.error(f"Failed to load schedule: {e}")
    return dict(DEFAULT_SCHEDULE)


def save_schedule(schedule: dict):
    """Save schedule to JSON file atomically."""
    temp_path = None
    try:
        SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(
            dir=SCHEDULE_FILE.parent,
            prefix="schedule_",
            suffix=".tmp",
        )
        with os.fdopen(temp_fd, "w") as f:
            json.dump(schedule, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, SCHEDULE_FILE)
        temp_path = None
    except OSError as e:
        logger.error(f"Failed to save schedule to {SCHEDULE_FILE}: {e}")
        raise
    finally:
        # Never leave the temp file behind when the write failed.
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


async def update_schedule_settings(patch: dict) -> dict:
    """Merge the user-facing keys of ``patch`` into the persisted schedule.

    Runs under the same lock as the scheduler tick, so settings written while
    a tick is in flight can never be clobbered by the tick's stale
    read-modify-write cycle (and vice versa). Only SCHEDULE_SETTING_KEYS are
    merged — anything else in the patch (notably ``auto_paused_campaigns``)
    is ignored. Returns the merged schedule; raises OSError on write failure
    (the API layer translates it).
    """
    async with _SCHED_LOCK:
        schedule = load_schedule()
        for key in SCHEDULE_SETTING_KEYS:
            if key in patch:
                schedule[key] = patch[key]
        save_schedule(schedule)
        return schedule


def is_within_send_window(schedule: dict) -> bool:
    """Check if current time is within the allowed send window."""
    tz = ZoneInfo(schedule["timezone"])
    now = datetime.now(tz)

    # Check day of week
    day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    allowed_days = {day_map[d] for d in (schedule.get("days") or []) if d in day_map}

    # Check time window
    start = time(schedule["start_hour"], schedule["start_minute"])
    end = time(schedule["end_hour"], schedule["end_minute"])
    current = now.time()

    if start <= end:
        if now.weekday() not in allowed_days:
            return False
        return start <= current <= end
    else:
        # Overnight window (e.g., 20:00 to 08:00)
        # If current time is after start, the window began today.
        # If current time is before end, the window began yesterday!
        if current >= start:
            return now.weekday() in allowed_days
        elif current <= end:
            yesterday_weekday = (now.weekday() - 1) % 7
            return yesterday_weekday in allowed_days
        else:
            return False


def _persist_auto_paused(schedule: dict, auto_paused: set, campaign_id: int) -> None:
    """Persist the tracking list right after each successful pause.

    Waiting for the end-of-tick save meant a save failure (disk
    full, permissions) left a campaign paused in ListMonk without
    a tracking record — the resume branch only resumes campaigns
    in ``auto_paused_campaigns``, so it would stay stalled until a
    human noticed.
    """
    schedule["auto_paused_campaigns"] = list(auto_paused)
    try:
        save_schedule(schedule)
    except OSError as e:
        logger.error(
            f"Scheduler could not persist auto_paused_campaigns "
            f"after pausing campaign #{campaign_id}: {e}. The "
            "campaign is paused upstream but not tracked for "
            "auto-resume."
        )


async def run_scheduler_tick(client: ListMonkClient):
    """Single tick of the scheduler. Called every 60 seconds.

    The entire load → mutate → save cycle runs under ``_SCHED_LOCK`` so the
    60-second loop, a PUT-triggered tick, and POST /api/scheduler/run cannot
    interleave and clobber each other's schedule writes.
    """
    async with _SCHED_LOCK:
        schedule = load_schedule()
        warm = warmup.load_state()
        window_on = bool(schedule.get("enabled"))
        warmup_on = bool(warm.get("enabled"))

        if not window_on and not warmup_on:
            # Nothing is enforced while disabled — but campaigns the scheduler
            # itself paused used to stay paused forever (only an enabled,
            # in-window tick resumes them), indistinguishable from a manual
            # pause. Release them instead.
            if schedule.get("auto_paused_campaigns"):
                await _release_auto_paused(client, schedule)
            return

        if window_on:
            # A schedule with no usable days is never "in window", which would make
            # this tick pause every running campaign and never resume any of them.
            # The API rejects an empty day list, so this only guards a hand-edited or
            # corrupted schedule file — skip the tick rather than stall all sending.
            day_map = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
            if not day_map.intersection(schedule.get("days", [])):
                logger.error(
                    "Schedule has no valid days configured; skipping this tick so "
                    "running campaigns are not paused indefinitely."
                )
                return

        # A campaign may send only when every enforced rule allows it. A rule
        # that is switched off never blocks.
        in_window = is_within_send_window(schedule) if window_on else True
        quota_left = warmup.has_quota(warm) if warmup_on else True
        may_send = in_window and quota_left
        auto_paused = set(schedule.get("auto_paused_campaigns") or [])

        tz = ZoneInfo(schedule["timezone"])
        now = datetime.now(tz)
        logger.debug(f"Scheduler tick: {now.strftime('%A %H:%M %Z')} | in_window={in_window} | quota_left={quota_left} | auto_paused={auto_paused}")

        try:
            # Only running/paused campaigns are relevant: running ones may need
            # pausing, and paused ones may need resuming. This avoids paginating
            # the entire campaign history every 60 seconds.
            campaigns = []
            for status in ("running", "paused"):
                campaigns.extend(
                    await client.paginate_all(
                        client.get_campaigns, per_page=100, status=status,
                    )
                )

            changed = False

            for camp in campaigns:
                cid = camp["id"]
                status = camp["status"]

                if not may_send and status == "running":
                    # Outside send window / warm-up quota used up -> pause it
                    try:
                        await client.change_campaign_status(cid, "paused")
                        auto_paused.add(cid)
                        camp["status"] = "paused"
                        changed = True
                        _persist_auto_paused(schedule, auto_paused, cid)
                        if not in_window:
                            logger.info(f"Auto-PAUSED campaign #{cid} '{camp['name']}' (outside {schedule['start_hour']:02d}:{schedule['start_minute']:02d}-{schedule['end_hour']:02d}:{schedule['end_minute']:02d} {schedule['timezone']})")
                        else:
                            logger.info(f"Auto-PAUSED campaign #{cid} '{camp['name']}' (daily warm-up quota used up)")
                    except Exception as e:
                        logger.error(f"Failed to pause campaign #{cid}: {e}")

                elif may_send and status == "paused" and cid in auto_paused:
                    # Allowed again + was auto-paused -> resume it
                    try:
                        await client.change_campaign_status(cid, "running")
                        auto_paused.discard(cid)
                        camp["status"] = "running"
                        changed = True
                        logger.info(f"Auto-RESUMED campaign #{cid} '{camp['name']}' (send window open, warm-up quota available)")
                    except Exception as e:
                        logger.error(f"Failed to resume campaign #{cid}: {e}")

            # Clean up auto_paused list: remove any that are no longer paused or no longer exist
            camp_map = {c["id"]: c for c in campaigns}
            for cid in list(auto_paused):
                camp = camp_map.get(cid)
                if not camp or camp["status"] != "paused":
                    auto_paused.discard(cid)
                    changed = True

            if changed:
                schedule["auto_paused_campaigns"] = list(auto_paused)
                save_schedule(schedule)

        except Exception as e:
            logger.error(f"Scheduler tick error: {e}")


async def _release_auto_paused(client: ListMonkClient, schedule: dict) -> None:
    """Resume the campaigns this scheduler paused (caller holds _SCHED_LOCK).

    Only campaigns still ``paused`` are resumed: one that was cancelled,
    finished, deleted or already resumed by hand is simply dropped from the
    tracking list. A failed resume stays tracked, so the next tick retries.
    """
    tracked = list(schedule.get("auto_paused_campaigns") or [])
    try:
        paused = {
            c.get("id"): c for c in await client.paginate_all(
                client.get_campaigns, per_page=100, status="paused",
            )
        }
    except Exception as e:
        logger.error(f"Scheduler disabled, but paused campaigns could not be listed: {e}")
        return

    still_tracked = []
    for cid in tracked:
        if cid not in paused:
            continue
        try:
            await client.change_campaign_status(cid, "running")
            logger.info(
                f"Auto-RESUMED campaign #{cid} '{paused[cid].get('name', '')}' "
                "(scheduler disabled)"
            )
        except Exception as e:
            still_tracked.append(cid)
            logger.error(f"Failed to resume campaign #{cid} after disabling the scheduler: {e}")

    if still_tracked != tracked:
        schedule["auto_paused_campaigns"] = still_tracked
        try:
            save_schedule(schedule)
        except OSError as e:
            logger.error(f"Scheduler could not persist auto_paused_campaigns: {e}")


async def scheduler_loop(client: ListMonkClient):
    """Background loop that runs the scheduler every 60 seconds."""
    while True:
        try:
            await run_scheduler_tick(client)
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")
        await asyncio.sleep(60)


# ── Warm-up enforcement (fast path) ──────────────────────────────────────────
# ListMonk sends several emails per second, so a 60 s tick would let a whole
# day's quota (or far more) slip out before it noticed. While a campaign is
# running the quota is therefore checked every second; with nothing running a
# slower poll is enough, and starting/resuming a campaign through this app
# wakes the loop immediately (nudge_warmup). The cap can still be exceeded by
# what ListMonk sends between two polls — roughly one second of sending.
WARMUP_ACTIVE_POLL = 1.0
WARMUP_IDLE_POLL = 3.0
WARMUP_MAX_BACKOFF = 30.0

# One wake-up event per running loop. Created lazily because an asyncio.Event
# binds to the loop that first waits on it, and every test runs on a new one.
_warmup_nudge: Optional[asyncio.Event] = None
_warmup_nudge_loop: Optional[asyncio.AbstractEventLoop] = None


def _nudge_event() -> asyncio.Event:
    global _warmup_nudge, _warmup_nudge_loop
    loop = asyncio.get_running_loop()
    if _warmup_nudge is None or _warmup_nudge_loop is not loop:
        _warmup_nudge = asyncio.Event()
        _warmup_nudge_loop = loop
    return _warmup_nudge


def nudge_warmup() -> None:
    """Wake the warm-up loop now instead of at its next poll."""
    try:
        _nudge_event().set()
    except RuntimeError:
        # No running event loop (called from a plain thread / test): the
        # loop's regular poll picks the change up within seconds anyway.
        pass


def reset_warmup_nudge() -> None:
    """Drop the cached wake-up event (tests; AGENTS.md rule 11)."""
    global _warmup_nudge, _warmup_nudge_loop
    _warmup_nudge = None
    _warmup_nudge_loop = None


async def _pause_for_quota(client: ListMonkClient, campaign_ids: list[int]) -> None:
    """Pause the running campaigns and track them for the next day's resume.

    Runs under ``_SCHED_LOCK`` and records the pauses in the same
    ``auto_paused_campaigns`` list the tick uses, so a manual pause is never
    mistaken for one of ours and the tick resumes these when the quota resets.
    """
    async with _SCHED_LOCK:
        schedule = load_schedule()
        auto_paused = set(schedule.get("auto_paused_campaigns") or [])
        for cid in campaign_ids:
            try:
                await client.change_campaign_status(cid, "paused")
            except Exception as e:
                # Typically a human paused it between the poll and now.
                logger.error(f"Failed to pause campaign #{cid} for the warm-up quota: {e}")
                continue
            auto_paused.add(cid)
            _persist_auto_paused(schedule, auto_paused, cid)
            logger.info(f"Auto-PAUSED campaign #{cid} (daily warm-up quota reached)")


def _send_stats_timezone() -> str:
    """The day boundary for the daily send statistics.

    The warm-up timezone while warm-up is on (so "sent today" matches the
    warm-up meter), otherwise the send-window timezone.
    """
    warm = warmup.load_state()
    if warm["enabled"]:
        return warm["timezone"]
    return load_schedule().get("timezone") or send_stats.DEFAULT_TIMEZONE


async def enforce_warmup(client: ListMonkClient) -> float:
    """One iteration of the warm-up loop; returns the seconds to wait next.

    The same poll also feeds the always-on daily send statistics, so it runs
    whether warm-up is on or not; a statistics failure never blocks quota
    enforcement.
    """
    stats = await warmup.fetch_running(client)
    try:
        await send_stats.observe(client, stats, _send_stats_timezone())
    except Exception as e:
        logger.warning(f"Send statistics update failed: {e}")
    snap = await warmup.observe(client, stats=stats)
    if not snap["enabled"]:
        # Counting is delta-based, so the statistics lose nothing at the
        # slower pace; only quota enforcement needs the 1 s poll.
        return WARMUP_IDLE_POLL
    if snap["rolled"]:
        # A new day began: campaigns held back by yesterday's quota may go on
        # now rather than at the next 60 s tick.
        await run_scheduler_tick(client)
    elif snap["running"] and snap["remaining"] <= 0:
        await _pause_for_quota(client, snap["running"])
    return WARMUP_ACTIVE_POLL if snap["running"] else WARMUP_IDLE_POLL


async def _sleep_or_nudge(delay: float) -> None:
    event = _nudge_event()
    try:
        await asyncio.wait_for(event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass
    # Cleared after waking, not before waiting: a nudge that arrived while the
    # last iteration was still running must trigger the next one immediately.
    event.clear()


async def warmup_loop(client: ListMonkClient):
    """Background loop enforcing the daily warm-up quota (see above)."""
    failures = 0
    while True:
        try:
            delay = await enforce_warmup(client)
            failures = 0
        except Exception as e:
            failures += 1
            logger.error(f"Warm-up loop error (#{failures}): {e}")
            # Back off while ListMonk is unreachable instead of logging and
            # hammering it every second.
            delay = min(WARMUP_MAX_BACKOFF, WARMUP_IDLE_POLL * 2 ** min(failures, 5))
        await _sleep_or_nudge(delay)
