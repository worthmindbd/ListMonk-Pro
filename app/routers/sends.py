"""Daily send statistics ("sent today", sends per day) — see services/send_stats."""

import asyncio
import logging

import httpx
from fastapi import APIRouter, Query

from app.services import send_stats
from app.services.deliverability_service import sending_window
from app.services.listmonk_client import listmonk

logger = logging.getLogger("sends")

router = APIRouter()

MAX_DAYS = 365
# Today's split never names more campaigns than this (one lookup each).
MAX_NAMED_CAMPAIGNS = 25
# A running campaign is "stalled" after this long without a send when no
# sliding window is configured (the engine then sends every few seconds).
STALL_SECONDS_NO_WINDOW = 15 * 60
# Margin on top of two sliding windows before a flat count is called a stall.
STALL_MARGIN_SECONDS = 5 * 60
# Used when ListMonk's settings cannot be read: late rather than a false alarm.
STALL_SECONDS_UNKNOWN = 2 * 3600


async def _campaign_label(campaign_id: int) -> dict:
    """``{"name", "deleted"}`` of a campaign; never raises."""
    try:
        data = (await listmonk.get_campaign(campaign_id)).get("data") or {}
        return {"name": data.get("name") or f"Campaign #{campaign_id}", "deleted": False}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"name": f"Deleted campaign #{campaign_id}", "deleted": True}
        logger.warning(f"Could not name campaign #{campaign_id}: {e}")
    except Exception as e:
        logger.warning(f"Could not name campaign #{campaign_id}: {e}")
    return {"name": f"Campaign #{campaign_id}", "deleted": None}


@router.get("/daily")
async def get_daily_sends(days: int = Query(30, ge=1, le=MAX_DAYS)):
    """Campaign emails sent per calendar day, oldest first, plus today's split.

    Days before counting started carry ``tracked: false`` — ListMonk keeps no
    per-day history, so those days are unknown rather than zero.

    Each entry of ``today_by_campaign`` carries the campaign ``name`` and
    ``deleted``: sends made today by a campaign that has since been deleted
    still happened (they count against the day and the warm-up quota), but
    ListMonk no longer lists them, so without the label "N sent today" looked
    like a phantom number next to campaigns that had sent nothing.
    """
    out = send_stats.daily(days)
    split = out["today_by_campaign"][:MAX_NAMED_CAMPAIGNS]
    labels = await asyncio.gather(*(_campaign_label(c["id"]) for c in split))
    for entry, label in zip(split, labels):
        entry.update(label)
    out["sent_today_by_deleted"] = sum(c["sent"] for c in split if c.get("deleted"))
    return out


@router.get("/progress")
async def get_send_progress():
    """Live progress of the running campaigns, with stall detection.

    ``campaigns[].stalled`` is true when a running campaign has sent nothing
    for longer than ListMonk's own pacing can explain (two sliding windows
    plus a margin). ``last_poll`` is when the app last read ListMonk's live
    counters (every few seconds while it runs).
    """
    threshold = STALL_SECONDS_UNKNOWN
    try:
        res = await listmonk.get_settings()
        window = sending_window(res.get("data") or {})
        threshold = (
            2 * window["seconds"] + STALL_MARGIN_SECONDS if window["active"]
            else STALL_SECONDS_NO_WINDOW
        )
    except Exception as e:
        logger.warning(f"Send progress: ListMonk settings unavailable, using {threshold}s: {e}")
    out = send_stats.progress(threshold)
    stalled = [c for c in out["campaigns"] if c["stalled"]]
    labels = await asyncio.gather(*(_campaign_label(c["id"]) for c in stalled))
    for entry, label in zip(stalled, labels):
        entry["name"] = label["name"]
    return out
