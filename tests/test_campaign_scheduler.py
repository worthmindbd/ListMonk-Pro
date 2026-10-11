from datetime import datetime, time
from zoneinfo import ZoneInfo
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
    monkeypatch.setattr("app.main.load_schedule", sched_module.load_schedule)
    monkeypatch.setattr("app.main.save_schedule", sched_module.save_schedule)

    # Test that 3-letter abbreviations sent from frontend are accepted
    data = {
        "enabled": False,
        "days": ["mon", "WED", "Friday"],
    }
    result = await update_schedule(data)
    assert result["status"] == "ok"
    assert result["schedule"]["days"] == ["mon", "wed", "fri"]
