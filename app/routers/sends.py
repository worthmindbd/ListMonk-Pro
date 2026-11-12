"""Daily send statistics ("sent today", sends per day) — see services/send_stats."""

from fastapi import APIRouter, Query

from app.services import send_stats

router = APIRouter()

MAX_DAYS = 365


@router.get("/daily")
async def get_daily_sends(days: int = Query(30, ge=1, le=MAX_DAYS)):
    """Campaign emails sent per calendar day, oldest first, plus today's split.

    Days before counting started carry ``tracked: false`` — ListMonk keeps no
    per-day history, so those days are unknown rather than zero.
    """
    return send_stats.daily(days)
