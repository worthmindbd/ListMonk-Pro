from datetime import datetime
from zoneinfo import ZoneInfo
import asyncio
import pytest
from unittest.mock import patch

from app.services.campaign_scheduler import is_within_send_window, save_schedule, load_schedule


def test_daytime_window():
    schedule = {
        "enabled": True,
        "timezone": "UTC",
        "start_hour": 9,
        "start_minute": 0,
        "end_hour": 17,
        "end_minute": 0,
        "days": ["mon", "tue", "wed", "thu", "fri"],
    }

    # Monday 12:00 UTC -> inside
    dt_mon_noon = datetime(2026, 3, 9, 12, 0, tzinfo=ZoneInfo("UTC"))
    with patch("app.services.campaign_scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = dt_mon_noon
        assert is_within_send_window(schedule) is True

    # Monday 18:00 UTC -> outside
    dt_mon_evening = datetime(2026, 3, 9, 18, 0, tzinfo=ZoneInfo("UTC"))
    with patch("app.services.campaign_scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = dt_mon_evening
        assert is_within_send_window(schedule) is False

    # Sunday 12:00 UTC -> outside (Sunday not in days)
    dt_sun_noon = datetime(2026, 3, 8, 12, 0, tzinfo=ZoneInfo("UTC"))
    with patch("app.services.campaign_scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = dt_sun_noon
        assert is_within_send_window(schedule) is False


def test_overnight_window():
    schedule = {
        "enabled": True,
        "timezone": "UTC",
        "start_hour": 21,
        "start_minute": 0,
        "end_hour": 5,
        "end_minute": 0,
        "days": ["mon", "tue", "wed", "thu", "fri"],
    }

    # Friday 22:00 UTC -> inside (Friday is active)
    dt_fri_night = datetime(2026, 3, 13, 22, 0, tzinfo=ZoneInfo("UTC"))
    with patch("app.services.campaign_scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = dt_fri_night
        assert is_within_send_window(schedule) is True

    # Saturday 03:00 UTC -> inside (started Friday night which was active!)
    dt_sat_early = datetime(2026, 3, 14, 3, 0, tzinfo=ZoneInfo("UTC"))
    with patch("app.services.campaign_scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = dt_sat_early
        assert is_within_send_window(schedule) is True

    # Saturday 06:00 UTC -> outside
    dt_sat_morning = datetime(2026, 3, 14, 6, 0, tzinfo=ZoneInfo("UTC"))
    with patch("app.services.campaign_scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = dt_sat_morning
        assert is_within_send_window(schedule) is False

    # Sunday 03:00 UTC -> outside (started Saturday night, which was NOT active)
    dt_sun_early = datetime(2026, 3, 15, 3, 0, tzinfo=ZoneInfo("UTC"))
    with patch("app.services.campaign_scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = dt_sun_early
        assert is_within_send_window(schedule) is False

    # Monday 03:00 UTC -> outside (started Sunday night, which was NOT active)
    dt_mon_early = datetime(2026, 3, 16, 3, 0, tzinfo=ZoneInfo("UTC"))
    with patch("app.services.campaign_scheduler.datetime") as mock_dt:
        mock_dt.now.return_value = dt_mon_early
        assert is_within_send_window(schedule) is False


def test_atomic_save_schedule(tmp_path, monkeypatch):
    import app.services.campaign_scheduler as sched_module
    sched_file = tmp_path / "schedule.json"
    monkeypatch.setattr(sched_module, "SCHEDULE_FILE", sched_file)

    data = {
        "enabled": True,
        "timezone": "UTC",
        "start_hour": 10,
        "start_minute": 15,
        "end_hour": 18,
        "end_minute": 45,
        "days": ["mon"],
        "auto_paused_campaigns": [123],
    }
    save_schedule(data)
    loaded = load_schedule()
    assert loaded["enabled"] is True
    assert loaded["start_hour"] == 10
    assert loaded["auto_paused_campaigns"] == [123]


@pytest.mark.asyncio
async def test_update_schedule_endpoint_normalizes_days(tmp_path, monkeypatch):
    import app.services.campaign_scheduler as sched_module
    from app.main import update_schedule

    sched_file = tmp_path / "schedule.json"
    monkeypatch.setattr(sched_module, "SCHEDULE_FILE", sched_file)
    # The PUT now persists through the service's locked
    # update_schedule_settings(); a fresh lock keeps this test's event loop
    # independent (same reasoning as the `sched` fixture below).
    monkeypatch.setattr(sched_module, "_SCHED_LOCK", asyncio.Lock())

    # Test that 3-letter abbreviations sent from frontend are accepted
    data = {
        "enabled": False,
        "days": ["mon", "WED", "Friday"],
    }
    result = await update_schedule(data)
    assert result["status"] == "ok"
    assert result["schedule"]["days"] == ["mon", "wed", "fri"]


# ── Tick serialisation and clobber-free settings updates ─────────────────────

class _SlowClient:
    """ListMonk double with a slow campaign walk that tracks concurrency."""

    def __init__(self, campaigns=None):
        self.campaigns = campaigns or []
        self.active = 0
        self.max_active = 0
        self.paginate_calls = 0
        self.status_calls = []

    async def paginate_all(self, fn, per_page=100, **kwargs):
        self.paginate_calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.05)
            status = kwargs.get("status")
            return [c for c in self.campaigns if c.get("status") == status]
        finally:
            self.active -= 1

    async def get_campaigns(self, **kwargs):
        return {"data": {"results": self.campaigns, "total": len(self.campaigns)}}

    async def change_campaign_status(self, cid, status):
        self.status_calls.append((cid, status))
        for c in self.campaigns:
            if c["id"] == cid:
                c["status"] = status
        return {"data": True}


@pytest.fixture
def sched(tmp_path, monkeypatch):
    """Scheduler module with its state file redirected into tmp."""
    import app.services.campaign_scheduler as sched_module
    monkeypatch.setattr(sched_module, "SCHEDULE_FILE", tmp_path / "schedule.json")
    # Fresh lock per test: a contended asyncio.Lock binds itself to the event
    # loop that first used it, and pytest-asyncio runs every test on a new
    # loop — reusing the import-time lock leaks one test's loop into the next.
    monkeypatch.setattr(sched_module, "_SCHED_LOCK", asyncio.Lock())
    return sched_module


def _always_in_window_schedule() -> dict:
    return {
        "enabled": True,
        "timezone": "UTC",
        "start_hour": 0,
        "start_minute": 0,
        "end_hour": 23,
        "end_minute": 59,
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "auto_paused_campaigns": [],
    }


@pytest.mark.asyncio
async def test_overlapping_ticks_serialize(sched):
    """The 60s loop, a PUT tick, and POST /run must never interleave.

    Two concurrent read-modify-write cycles on schedule.json clobber each
    other (lost settings, campaigns that never auto-resume), so the whole
    tick body runs under a module lock.
    """
    sched.save_schedule(_always_in_window_schedule())
    client = _SlowClient()

    await asyncio.gather(*(sched.run_scheduler_tick(client) for _ in range(3)))

    assert client.paginate_calls == 6, "every tick must actually run"
    assert client.max_active == 1, "ticks must never overlap"


@pytest.mark.asyncio
async def test_update_during_in_flight_tick_is_not_clobbered(sched, monkeypatch):
    """A settings write while a tick is mid-flight survives the tick's save."""
    sched.save_schedule(_always_in_window_schedule())
    # Outside the window -> the tick pauses the running campaign and then
    # saves its (stale) copy of the schedule, which used to overwrite any
    # settings change made while the slow campaign walk was in flight.
    monkeypatch.setattr(sched, "is_within_send_window", lambda schedule: False)
    client = _SlowClient([{"id": 1, "name": "C", "status": "running"}])

    tick = asyncio.create_task(sched.run_scheduler_tick(client))
    await asyncio.sleep(0.01)  # tick grabs the lock and reaches the slow walk
    merged = await sched.update_schedule_settings(
        {"enabled": False, "start_hour": 9, "auto_paused_campaigns": [999], "junk": "x"}
    )
    await tick

    assert client.status_calls == [(1, "paused")]
    final = sched.load_schedule()
    assert final["enabled"] is False, "settings update was clobbered by the tick"
    assert final["start_hour"] == 9
    # Runtime state belongs to the tick: the patch's value is ignored, the
    # tick's own bookkeeping is kept.
    assert final["auto_paused_campaigns"] == [1]
    assert "junk" not in final
    assert merged["enabled"] is False


@pytest.mark.asyncio
async def test_update_schedule_settings_merges_only_whitelisted_keys(sched):
    sched.save_schedule(_always_in_window_schedule())

    result = await sched.update_schedule_settings(
        {"timezone": "Europe/Berlin", "days": ["mon"], "auto_paused_campaigns": [42], "junk": 1}
    )

    assert result["timezone"] == "Europe/Berlin"
    assert result["days"] == ["mon"]
    assert result["enabled"] is True, "untouched keys keep their stored value"
    assert result["auto_paused_campaigns"] == []
    assert "junk" not in result
    on_disk = sched.load_schedule()
    assert on_disk["timezone"] == "Europe/Berlin"
