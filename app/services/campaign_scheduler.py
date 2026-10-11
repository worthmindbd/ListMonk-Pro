"""
Campaign Scheduler: Auto-pause/resume campaigns based on send time windows.

Checks every 60 seconds:
- If current time is OUTSIDE the send window and campaign is running -> pause it
- If current time is INSIDE the send window and campaign was auto-paused -> resume it

Tracks which campaigns were auto-paused so it doesn't interfere with manual pauses.
"""

import asyncio
import json
import logging
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings
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


def load_schedule() -> dict:
    """Load schedule from JSON file."""
    if SCHEDULE_FILE.exists():
        try:
            with open(SCHEDULE_FILE) as f:
                data = json.load(f)
            # Merge with defaults for any missing keys
            merged = {**DEFAULT_SCHEDULE, **data}
            return merged
        except Exception as e:
            logger.error(f"Failed to load schedule: {e}")
    return dict(DEFAULT_SCHEDULE)


import os
import tempfile

def save_schedule(schedule: dict):
    """Save schedule to JSON file atomically."""
    try:
        SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_path = tempfile.mkstemp(
            dir=SCHEDULE_FILE.parent,
            prefix="schedule_",
            suffix=".tmp",
        )
        with os.fdopen(temp_fd, "w") as f:
            json.dump(schedule, f, indent=2)
        os.replace(temp_path, SCHEDULE_FILE)
    except OSError as e:
        logger.error(f"Failed to save schedule to {SCHEDULE_FILE}: {e}")
        raise


def is_within_send_window(schedule: dict) -> bool:
    """Check if current time is within the allowed send window."""
    tz = ZoneInfo(schedule["timezone"])
    now = datetime.now(tz)

    # Check day of week
    day_map = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    allowed_days = {day_map[d] for d in schedule.get("days", []) if d in day_map}

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


async def run_scheduler_tick(client: ListMonkClient):
    """Single tick of the scheduler. Called every 60 seconds."""
    schedule = load_schedule()

    if not schedule.get("enabled"):
        return

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

    in_window = is_within_send_window(schedule)
    auto_paused = set(schedule.get("auto_paused_campaigns", []))

    tz = ZoneInfo(schedule["timezone"])
    now = datetime.now(tz)
    logger.debug(f"Scheduler tick: {now.strftime('%A %H:%M %Z')} | in_window={in_window} | auto_paused={auto_paused}")

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

            if not in_window and status == "running":
                # Outside send window -> pause it
                try:
                    await client.change_campaign_status(cid, "paused")
                    auto_paused.add(cid)
                    camp["status"] = "paused"
                    changed = True
                    logger.info(f"Auto-PAUSED campaign #{cid} '{camp['name']}' (outside {schedule['start_hour']:02d}:{schedule['start_minute']:02d}-{schedule['end_hour']:02d}:{schedule['end_minute']:02d} {schedule['timezone']})")
                except Exception as e:
                    logger.error(f"Failed to pause campaign #{cid}: {e}")

            elif in_window and status == "paused" and cid in auto_paused:
                # Inside send window + was auto-paused -> resume it
                try:
                    await client.change_campaign_status(cid, "running")
                    auto_paused.discard(cid)
                    camp["status"] = "running"
                    changed = True
                    logger.info(f"Auto-RESUMED campaign #{cid} '{camp['name']}' (inside send window)")
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


async def scheduler_loop(client: ListMonkClient):
    """Background loop that runs the scheduler every 60 seconds."""
    while True:
        try:
            await run_scheduler_tick(client)
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")
        await asyncio.sleep(60)
