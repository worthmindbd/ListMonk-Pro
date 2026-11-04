"""Warm-up router: gradual daily send-volume ramp (see app/services/warmup.py).

Static routes only (``/preview``, ``/insights``) — there is no ``/{id}`` here,
so route order cannot shadow anything.
"""

import logging

from fastapi import APIRouter, HTTPException, Query

from app.services import warmup
from app.services.campaign_scheduler import nudge_warmup, run_scheduler_tick
from app.services.listmonk_client import listmonk

logger = logging.getLogger("warmup_router")
router = APIRouter()


def _positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


@router.get("")
async def get_warmup():
    """Ramp configuration, today's quota/usage, forecast and recent days."""
    return warmup.view(warmup.load_state())


@router.put("")
async def update_warmup(data: dict):
    """Change warm-up settings (strictly validated; unknown keys → 400).

    ``level`` jumps the ramp to a given day (e.g. a domain that is already
    partly warmed up, or 1 to start over). Ticks the scheduler right away so
    campaigns are held back or released without waiting for the next poll.
    """
    try:
        patch = warmup.validate_patch(data)
        await warmup.update_settings(patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        logger.error(f"Could not persist warm-up settings: {e}")
        raise HTTPException(
            status_code=500,
            detail="Could not write warm-up file (check data directory permissions).",
        )
    nudge_warmup()
    await run_scheduler_tick(listmonk)
    return {"status": "ok", "warmup": warmup.view(warmup.load_state())}


@router.get("/preview")
async def preview_ramp(
    start_volume: int = Query(10, ge=1, le=10_000),
    growth_percent: int = Query(20, ge=0, le=300),
    target_volume: int = Query(200, ge=1, le=100_000),
):
    """The ramp a set of values would produce, for the settings form's live chart."""
    if target_volume < start_volume:
        raise HTTPException(status_code=400, detail="target_volume must be at least start_volume")
    ramp = warmup.build_ramp(start_volume, growth_percent, target_volume)
    return {"ramp": ramp, "days": len(ramp), "total": sum(ramp)}


@router.get("/insights")
async def warmup_insights():
    """What the queue and ListMonk's send speed mean for the ramp.

    Everything here comes from ListMonk and is best effort: a failure leaves
    that part ``null`` instead of failing the request, so the page renders
    even while ListMonk is unreachable.
    """
    state = warmup.load_state()
    out = {
        "backlog": None, "days_to_finish": None,
        "message_rate": None, "concurrency": None,
        "max_per_second": None, "overshoot_warning": False,
    }

    try:
        backlog = 0
        for status in ("running", "paused"):
            for camp in await listmonk.paginate_all(listmonk.get_campaigns, per_page=100, status=status):
                backlog += max(0, (_positive_int(camp.get("to_send")) or 0) - (_positive_int(camp.get("sent")) or 0))
        out["backlog"] = backlog
        out["days_to_finish"] = warmup.estimate_days(state, backlog)
    except Exception as e:
        logger.warning(f"Warm-up insights: could not read the campaign queue: {e}")

    try:
        raw = (await listmonk.get_settings()).get("data") or {}
        rate = _positive_int(raw.get("app.message_rate"))
        concurrency = _positive_int(raw.get("app.concurrency"))
        out["message_rate"] = rate
        out["concurrency"] = concurrency
        if rate and concurrency:
            out["max_per_second"] = rate * concurrency
            # The cap is checked about once a second, so it can be exceeded by
            # roughly that many emails; flag it when that is a real share of
            # today's quota (an upper bound — SMTP latency usually slows it).
            out["overshoot_warning"] = out["max_per_second"] * 5 > warmup.view(state)["quota"]
    except Exception as e:
        logger.warning(f"Warm-up insights: could not read ListMonk settings: {e}")

    return out
