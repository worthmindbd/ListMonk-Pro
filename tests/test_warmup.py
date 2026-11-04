"""Sending warm-up: ramp maths, quota accounting, enforcement and the API.

The ListMonk double below mimics the three things warm-up depends on:
``/api/campaigns/running/stats`` (live cumulative ``sent`` of running
campaigns), the campaign lists, and a pause/resume that works like ListMonk's.
Time is passed in (``now=``) or patched (``warmup._now``) — never slept.
"""

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.services import campaign_scheduler as sched
from app.services import warmup
from tests.conftest import CSRFTestClient

UTC = ZoneInfo("UTC")
D1 = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
D2 = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
D3 = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
D6 = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)


class FakeListMonk:
    """Just enough ListMonk for the warm-up poll and the scheduler tick."""

    def __init__(self):
        self.campaigns: dict[int, dict] = {}
        self.status_calls: list[tuple[int, str]] = []
        self.final_lookup_fails = False
        self.calls = 0

    def add(self, cid, status="running", sent=0, to_send=1000):
        self.campaigns[cid] = {"id": cid, "name": f"C{cid}", "status": status,
                               "sent": sent, "to_send": to_send}

    def send(self, cid, n):
        self.campaigns[cid]["sent"] += n

    async def get_running_stats(self, campaign_id=None):
        self.calls += 1
        return {"data": [
            {"id": c["id"], "sent": c["sent"], "to_send": c["to_send"], "status": "running"}
            for c in self.campaigns.values() if c["status"] == "running"
        ]}

    async def get_campaigns(self, **kwargs):  # passed to paginate_all as the fetcher
        raise AssertionError("paginate_all is faked directly")

    async def paginate_all(self, fn, per_page=100, **kwargs):
        self.calls += 1
        return [dict(c) for c in self.campaigns.values() if c["status"] == kwargs.get("status")]

    async def get_campaign(self, cid):
        self.calls += 1
        if self.final_lookup_fails:
            raise httpx.ConnectError("boom")
        if cid not in self.campaigns:
            raise httpx.HTTPStatusError(
                "gone", request=httpx.Request("GET", "http://lm"),
                response=httpx.Response(404, request=httpx.Request("GET", "http://lm")),
            )
        return {"data": dict(self.campaigns[cid])}

    async def change_campaign_status(self, cid, status):
        self.status_calls.append((cid, status))
        self.campaigns[cid]["status"] = status
        return {"data": True}


@pytest.fixture
def lm():
    return FakeListMonk()


@pytest.fixture(autouse=True)
def _fresh_sched_lock(monkeypatch):
    # See test_campaign_scheduler.py: a lock binds to the loop that contends it.
    monkeypatch.setattr(sched, "_SCHED_LOCK", asyncio.Lock())


def enable(**over):
    """Persist an enabled, already-baselined warm-up (doubling 10 → 80)."""
    state = warmup.load_state()
    state.update(
        enabled=True, timezone="UTC", start_volume=10, growth_percent=100,
        target_volume=80, date="2026-11-04", baselined=True,
    )
    state.update(over)
    warmup.save_state(state)
    return state


def at(monkeypatch, moment):
    """Pin the clock the scheduler tick and views read."""
    monkeypatch.setattr(warmup, "_now", lambda tz: moment.astimezone(ZoneInfo(tz)))


# ── Ramp maths ───────────────────────────────────────────────────────────────

def test_doubling_ramp_matches_the_requested_example():
    ramp = warmup.build_ramp(10, 100, 1000)
    assert ramp[:5] == [10, 20, 40, 80, 160]
    assert ramp[-1] == 1000, "the last step lands exactly on the target"


def test_ramp_never_exceeds_the_target():
    assert warmup.build_ramp(10, 100, 50) == [10, 20, 40, 50]


def test_ramp_grows_by_at_least_one_a_day():
    # 5 × 1.10 rounds back to 5-6; without the floor it would stall.
    assert warmup.build_ramp(5, 0, 9) == [5, 6, 7, 8, 9]
    ramp = warmup.build_ramp(5, 10, 12)
    assert all(b > a for a, b in zip(ramp, ramp[1:]))


def test_start_above_target_collapses_to_the_target():
    assert warmup.build_ramp(50, 20, 10) == [10]


def test_every_preset_is_a_strictly_rising_ramp():
    for key, preset in warmup.RAMP_PRESETS.items():
        ramp = warmup.build_ramp(preset["start_volume"], preset["growth_percent"], 500)
        assert ramp[-1] == 500, key
        assert all(b > a for a, b in zip(ramp, ramp[1:])), key
    gentle = len(warmup.build_ramp(10, 10, 500))
    standard = len(warmup.build_ramp(10, 20, 500))
    doubling = len(warmup.build_ramp(10, 100, 500))
    assert gentle > standard > doubling


def test_ramp_length_is_bounded():
    assert len(warmup.build_ramp(1, 0, 100_000)) == warmup.MAX_RAMP_DAYS


# ── Validation ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    {}, [], "x", None,
    {"junk": 1},
    {"enabled": "true"}, {"enabled": 1},
    {"start_volume": True}, {"start_volume": 0}, {"start_volume": 1.5}, {"start_volume": "10"},
    {"start_volume": 10_001},
    {"growth_percent": -1}, {"growth_percent": 301}, {"growth_percent": 12.5},
    {"target_volume": 0}, {"target_volume": 100_001},
    {"timezone": "Mars/Base"}, {"timezone": 5}, {"timezone": ""},
    {"level": 0}, {"level": warmup.MAX_RAMP_DAYS + 1}, {"level": False},
])
def test_validate_patch_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        warmup.validate_patch(bad)


def test_validate_patch_accepts_the_documented_shape():
    patch_ = {"enabled": True, "timezone": "Asia/Dhaka", "start_volume": 10,
              "growth_percent": 0, "target_volume": 500, "level": 3}
    assert warmup.validate_patch(patch_) == patch_


async def test_update_settings_rejects_target_below_start():
    with pytest.raises(ValueError, match="target_volume"):
        await warmup.update_settings({"start_volume": 100, "target_volume": 50})
    assert warmup.load_state()["start_volume"] == 10, "a rejected patch writes nothing"


async def test_update_settings_clamps_level_to_the_ramp_and_keeps_runtime_state():
    enable(sent_today=4, level=2)
    state = await warmup.update_settings({"level": 99})
    assert state["level"] == len(warmup.ramp_for(state))
    assert state["sent_today"] == 4, "changing the level never wipes today's count"


async def test_toggling_warmup_retakes_the_baseline():
    enable(seen={"7": 500}, running=[7])
    state = await warmup.update_settings({"enabled": False})
    assert state["baselined"] is False and state["running"] == []
    state = await warmup.update_settings({"enabled": True})
    assert state["baselined"] is False


async def test_changing_timezone_does_not_close_the_day():
    enable(date="2026-11-04", sent_today=9, level=1)
    # 15:00 UTC on Nov 4 is 04:00 on Nov 5 in Auckland (UTC+13): switching zones
    # moves "today" without a day having passed, so the counter must follow it
    # rather than be closed out as a finished, half-used day.
    moment = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)
    state = await warmup.update_settings({"timezone": "Pacific/Auckland"}, now=moment)
    assert state["date"] == "2026-11-05"
    assert state["sent_today"] == 9 and state["level"] == 1 and state["history"] == []
    assert warmup._roll_day(state, moment) is False, "the next poll must not close the day either"


# ── State file ───────────────────────────────────────────────────────────────

def test_load_state_survives_a_corrupted_file(tmp_path):
    warmup.WARMUP_FILE.write_text(json.dumps({
        "enabled": "yes", "timezone": None, "start_volume": "ten",
        "growth_percent": None, "target_volume": 5, "level": -3, "sent_today": "x",
        "date": "not-a-date", "baselined": "true",
        "seen": {"1": 5, "2": "x", "3": -1, "4": True},
        "running": [1, "2", None, True], "history": ["junk", {"date": "2026-01-01"}],
    }), encoding="utf-8")
    state = warmup.load_state()
    assert state["enabled"] is False and state["timezone"] == "UTC"
    assert state["start_volume"] == 10 and state["growth_percent"] == 20
    assert state["target_volume"] >= state["start_volume"]
    assert state["level"] == 1 and state["sent_today"] == 0 and state["date"] is None
    assert state["baselined"] is False
    assert state["seen"] == {"1": 5}
    assert state["running"] == [1]
    assert state["history"] == [{"date": "2026-01-01"}]
    # And the view over it must not raise either.
    warmup.view(state)


def test_load_state_with_a_non_object_or_garbage_file():
    warmup.WARMUP_FILE.write_text("[1, 2]", encoding="utf-8")
    assert warmup.load_state()["enabled"] is False
    warmup.WARMUP_FILE.write_text("{not json", encoding="utf-8")
    assert warmup.load_state()["enabled"] is False


def test_save_state_round_trips_atomically():
    state = enable(sent_today=3)
    assert warmup.load_state() == state
    assert not list(warmup.WARMUP_FILE.parent.glob("warmup_*.tmp"))


# ── Day roll-over ────────────────────────────────────────────────────────────

def test_day_advances_after_a_day_that_used_enough_of_its_quota():
    state = enable(sent_today=8, level=1)            # 8/10 = exactly the 80 % bar
    assert warmup._roll_day(state, D2) is True
    assert state["level"] == 2 and state["sent_today"] == 0 and state["date"] == "2026-11-05"
    assert state["history"] == [{"date": "2026-11-04", "day": 1, "quota": 10, "sent": 8, "advanced": True}]


def test_ramp_holds_after_a_day_that_fell_short():
    state = enable(sent_today=7, level=1)
    warmup._roll_day(state, D2)
    assert state["level"] == 1
    assert state["history"][-1]["advanced"] is False


def test_ramp_holds_through_idle_days():
    """A weekend / empty queue / pause must not push the volume up."""
    state = enable(sent_today=10, level=1)
    warmup._roll_day(state, D2)                      # day 1 done → level 2
    for later in (D3, D6):                           # nothing sent on the days after
        warmup._roll_day(state, later)
    assert state["level"] == 2


def test_a_long_gap_closes_only_one_day():
    state = enable(sent_today=10, level=1)
    warmup._roll_day(state, D6)                      # app was down four days
    assert state["level"] == 2 and len(state["history"]) == 1


def test_same_day_does_not_roll():
    state = enable(sent_today=5)
    assert warmup._roll_day(state, D1) is False
    assert state["sent_today"] == 5


def test_level_never_passes_the_end_of_the_ramp():
    ramp_len = len(warmup.build_ramp(10, 100, 80))
    state = enable(sent_today=80, level=ramp_len)
    warmup._roll_day(state, D2)
    assert state["level"] == ramp_len


def test_view_reports_the_next_days_quota_before_the_roll_over_is_saved():
    state = enable(sent_today=10, level=1, date="2026-11-04")
    same_day = warmup.view(state, D1)
    assert same_day["remaining"] == 0 and same_day["state"] == "quota_reached"
    next_day = warmup.view(state, D2)
    assert next_day["pending_rollover"] is True
    assert (next_day["day"], next_day["quota"], next_day["sent_today"]) == (2, 20, 0)
    assert warmup.has_quota(state, D1) is False and warmup.has_quota(state, D2) is True


def test_view_when_off_and_when_idle_or_sending():
    assert warmup.view(warmup.load_state(), D1)["state"] == "off"
    assert warmup.view(enable(), D1)["state"] == "idle"
    assert warmup.view(enable(running=[3]), D1)["state"] == "sending"
    out = warmup.view(enable(), D1)
    assert out["next_quota"] == 20 and out["days_to_target"] == len(out["ramp"]) - 1
    assert [h["day"] for h in warmup.view(enable(history=[{"day": 1}, {"day": 2}]), D1)["history"]] == [2, 1]


def test_estimate_days_walks_the_ramp():
    state = enable(target_volume=200)                # 10, 20, 40, 80, 160, 200
    # 10 today + 20 + 40 + 80 = 150 ≥ 100 → done on the 3rd day after today.
    assert warmup.estimate_days(state, 100, D1) == 3
    assert warmup.estimate_days(state, 10, D1) == 0
    assert warmup.estimate_days(state, 0, D1) == 0


# ── Counting sends ───────────────────────────────────────────────────────────

async def test_observe_is_inert_while_warmup_is_off(lm):
    out = await warmup.observe(lm, D1)
    assert out["enabled"] is False
    assert lm.calls == 0, "a disabled warm-up must not touch ListMonk"


async def test_enabling_baselines_existing_progress_instead_of_charging_it(lm):
    lm.add(1, "running", sent=500)
    lm.add(2, "paused", sent=40)
    await warmup.update_settings({"enabled": True, "timezone": "UTC"}, now=D1)

    first = await warmup.observe(lm, D1)
    assert first["sent_today"] == 0, "history from before warm-up is not today's sending"
    assert warmup.load_state()["seen"] == {"1": 500, "2": 40}

    lm.send(1, 6)
    assert (await warmup.observe(lm, D1))["sent_today"] == 6


async def test_a_new_campaign_is_counted_from_its_first_message(lm):
    enable()
    lm.add(5, "running", sent=3)                     # already 3 out by the first poll
    assert (await warmup.observe(lm, D1))["sent_today"] == 3
    lm.send(5, 4)
    assert (await warmup.observe(lm, D1))["sent_today"] == 7


async def test_all_running_campaigns_share_one_counter(lm):
    enable()
    lm.add(1, "running")
    lm.add(2, "running")
    lm.send(1, 3)
    lm.send(2, 4)
    assert (await warmup.observe(lm, D1))["sent_today"] == 7


async def test_a_resumed_campaign_only_counts_new_sends(lm):
    enable(seen={"1": 30})
    lm.add(1, "paused", sent=30)
    await warmup.observe(lm, D1)
    lm.campaigns[1]["status"] = "running"
    lm.send(1, 2)
    assert (await warmup.observe(lm, D1))["sent_today"] == 2


async def test_sends_while_the_app_was_down_are_counted_on_the_next_poll(lm):
    enable()
    lm.add(1, "running")
    lm.send(1, 5)
    await warmup.observe(lm, D1)
    lm.send(1, 9)                                    # app restarted; ListMonk kept sending
    assert (await warmup.observe(lm, D1))["sent_today"] == 14


async def test_a_finished_campaigns_last_messages_are_not_lost(lm):
    enable()
    lm.add(1, "running")
    lm.send(1, 5)
    await warmup.observe(lm, D1)
    lm.send(1, 3)
    lm.campaigns[1]["status"] = "finished"           # left the running list before the next poll
    out = await warmup.observe(lm, D1)
    assert out["sent_today"] == 8
    assert "1" not in warmup.load_state()["seen"], "finished campaigns are forgotten"


async def test_a_deleted_campaign_is_dropped_without_error(lm):
    enable()
    lm.add(1, "running")
    lm.send(1, 2)
    await warmup.observe(lm, D1)
    del lm.campaigns[1]
    out = await warmup.observe(lm, D1)
    assert out["sent_today"] == 2
    assert warmup.load_state()["seen"] == {}


async def test_a_failed_final_lookup_is_retried_not_forgotten(lm):
    enable()
    lm.add(1, "running")
    lm.send(1, 5)
    await warmup.observe(lm, D1)
    lm.send(1, 3)
    lm.campaigns[1]["status"] = "finished"
    lm.final_lookup_fails = True
    assert (await warmup.observe(lm, D1))["sent_today"] == 5
    assert warmup.load_state()["unsettled"] == [1]
    lm.final_lookup_fails = False
    assert (await warmup.observe(lm, D1))["sent_today"] == 8
    assert warmup.load_state()["unsettled"] == []


async def test_observe_rolls_the_day_and_reports_it(lm):
    enable(sent_today=10, date="2026-11-04", level=1)
    out = await warmup.observe(lm, D2)
    assert out["rolled"] is True
    assert (out["quota"], out["sent_today"], out["remaining"]) == (20, 0, 20)


async def test_a_listmonk_outage_changes_nothing(lm):
    enable(sent_today=4)

    async def down(*a, **k):
        raise httpx.ConnectError("down")
    lm.get_running_stats = down
    with pytest.raises(httpx.ConnectError):
        await warmup.observe(lm, D1)
    assert warmup.load_state()["sent_today"] == 4


async def test_settings_changed_mid_poll_are_not_overwritten(lm):
    """The poll's network I/O is outside the lock; its write is a fresh read-modify-write."""
    enable()
    lm.add(1, "running")
    lm.send(1, 2)

    real = lm.get_running_stats

    async def slow_stats(*a, **k):
        data = await real(*a, **k)
        await warmup.update_settings({"target_volume": 500})   # lands during the request
        return data
    lm.get_running_stats = slow_stats

    await warmup.observe(lm, D1)
    state = warmup.load_state()
    assert state["target_volume"] == 500, "settings write was clobbered by the poll"
    assert state["sent_today"] == 2


def test_seen_is_bounded():
    state = enable()
    state["seen"] = {str(i): i for i in range(warmup.SEEN_LIMIT + 50)}
    warmup._apply_counts(state, [], {})
    assert len(state["seen"]) == warmup.SEEN_LIMIT
    assert "0" not in state["seen"], "the oldest (lowest id) entries go first"


# ── Enforcement ──────────────────────────────────────────────────────────────

async def test_quota_reached_pauses_the_running_campaign_and_tracks_it(lm):
    enable()
    lm.add(1, "running")
    lm.send(1, 10)                                   # exactly today's quota
    delay = await sched.enforce_warmup(lm)

    assert lm.status_calls == [(1, "paused")]
    assert sched.load_schedule()["auto_paused_campaigns"] == [1]
    assert delay == sched.WARMUP_ACTIVE_POLL


async def test_overshoot_between_polls_still_pauses(lm):
    enable()
    lm.add(1, "running")
    lm.send(1, 17)                                   # ListMonk sent a burst before we looked
    await sched.enforce_warmup(lm)
    assert lm.status_calls == [(1, "paused")]
    assert warmup.load_state()["sent_today"] == 17, "the overshoot is recorded, not hidden"


async def test_below_quota_nothing_is_paused(lm):
    enable()
    lm.add(1, "running")
    lm.send(1, 9)
    await sched.enforce_warmup(lm)
    assert lm.status_calls == []


async def test_every_running_campaign_is_paused_at_the_shared_quota(lm):
    enable()
    for cid in (1, 2):
        lm.add(cid, "running")
        lm.send(cid, 5)
    await sched.enforce_warmup(lm)
    assert sorted(lm.status_calls) == [(1, "paused"), (2, "paused")]
    assert sorted(sched.load_schedule()["auto_paused_campaigns"]) == [1, 2]


async def test_poll_interval_is_fast_only_while_something_runs(lm):
    enable()
    assert await sched.enforce_warmup(lm) == sched.WARMUP_IDLE_POLL
    lm.add(1, "running")
    assert await sched.enforce_warmup(lm) == sched.WARMUP_ACTIVE_POLL
    warmup.save_state({**warmup.load_state(), "enabled": False})
    assert await sched.enforce_warmup(lm) == sched.WARMUP_IDLE_POLL


async def test_a_failed_pause_is_not_tracked_as_ours(lm):
    enable()
    lm.add(1, "running")
    lm.send(1, 10)

    async def refuse(cid, status):
        raise httpx.HTTPError("campaign is already paused")
    lm.change_campaign_status = refuse
    await sched.enforce_warmup(lm)
    assert sched.load_schedule()["auto_paused_campaigns"] == []


async def test_tick_keeps_a_quota_paused_campaign_paused_all_day(lm, monkeypatch):
    enable()
    at(monkeypatch, D1)
    lm.add(1, "running")
    lm.send(1, 10)
    await sched.enforce_warmup(lm)
    lm.status_calls.clear()

    await sched.run_scheduler_tick(lm)               # the 60 s tick, same day
    assert lm.status_calls == [], "must not resume while the quota is used up"
    assert lm.campaigns[1]["status"] == "paused"


async def test_tick_resumes_the_campaign_when_a_new_day_starts(lm, monkeypatch):
    enable()
    at(monkeypatch, D1)
    lm.add(1, "running")
    lm.send(1, 10)
    await sched.enforce_warmup(lm)
    lm.status_calls.clear()

    at(monkeypatch, D2)
    await sched.run_scheduler_tick(lm)
    assert lm.status_calls == [(1, "running")]
    assert sched.load_schedule()["auto_paused_campaigns"] == []


async def test_the_roll_over_resumes_immediately_instead_of_waiting_for_the_tick(lm, monkeypatch):
    enable()
    at(monkeypatch, D1)
    lm.add(1, "running")
    lm.send(1, 10)
    await sched.enforce_warmup(lm)

    at(monkeypatch, D2)
    await sched.enforce_warmup(lm)                   # first poll of the new day
    assert lm.campaigns[1]["status"] == "running"
    assert warmup.load_state()["level"] == 2


async def test_a_manually_paused_campaign_is_never_resumed(lm, monkeypatch):
    enable()
    at(monkeypatch, D2)
    lm.add(1, "paused")                              # a human paused it; not in auto_paused
    await sched.run_scheduler_tick(lm)
    assert lm.status_calls == []


async def test_send_window_and_quota_must_both_allow_a_resume(lm, monkeypatch):
    """Quota is fine, but the window is closed → the campaign stays paused."""
    enable()
    at(monkeypatch, D2)
    sched.save_schedule({**sched.DEFAULT_SCHEDULE, "enabled": True, "auto_paused_campaigns": [1]})
    monkeypatch.setattr(sched, "is_within_send_window", lambda s: False)
    lm.add(1, "paused")
    await sched.run_scheduler_tick(lm)
    assert lm.status_calls == []

    monkeypatch.setattr(sched, "is_within_send_window", lambda s: True)
    await sched.run_scheduler_tick(lm)
    assert lm.status_calls == [(1, "running")]


async def test_window_open_but_quota_used_pauses_a_running_campaign(lm, monkeypatch):
    enable()
    at(monkeypatch, D1)
    sched.save_schedule({**sched.DEFAULT_SCHEDULE, "enabled": True})
    monkeypatch.setattr(sched, "is_within_send_window", lambda s: True)
    warmup.save_state({**warmup.load_state(), "sent_today": 10})
    lm.add(1, "running")
    await sched.run_scheduler_tick(lm)
    assert lm.status_calls == [(1, "paused")]


async def test_turning_warmup_off_releases_what_it_paused(lm, monkeypatch):
    enable()
    at(monkeypatch, D1)
    lm.add(1, "running")
    lm.send(1, 10)
    await sched.enforce_warmup(lm)
    assert lm.campaigns[1]["status"] == "paused"

    await warmup.update_settings({"enabled": False})
    await sched.run_scheduler_tick(lm)
    assert lm.campaigns[1]["status"] == "running"


async def test_warmup_off_leaves_the_tick_exactly_as_before(lm):
    sched.save_schedule({**sched.DEFAULT_SCHEDULE, "enabled": False})
    lm.add(1, "running")
    await sched.run_scheduler_tick(lm)
    assert lm.status_calls == []


# ── Loop plumbing ────────────────────────────────────────────────────────────

async def test_the_loop_backs_off_on_errors_and_recovers(monkeypatch):
    outcomes = [RuntimeError("down"), RuntimeError("down"), 1.0]
    delays = []

    async def fake_enforce(client):
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    async def fake_sleep(delay):
        delays.append(delay)
        if len(delays) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(sched, "enforce_warmup", fake_enforce)
    monkeypatch.setattr(sched, "_sleep_or_nudge", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await sched.warmup_loop(object())
    assert delays[0] < delays[1] <= sched.WARMUP_MAX_BACKOFF
    assert delays[2] == 1.0, "a good iteration resets the back-off"


async def test_nudge_wakes_a_sleeping_loop_at_once():
    waiter = asyncio.create_task(sched._sleep_or_nudge(30))
    await asyncio.sleep(0.01)
    sched.nudge_warmup()
    await asyncio.wait_for(waiter, timeout=1)


def test_nudge_outside_an_event_loop_is_harmless():
    sched.nudge_warmup()


# ── API ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    from app.main import app
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app, raise_server_exceptions=False)


def test_get_returns_the_defaults_before_anything_is_configured(client):
    body = client.get("/api/warmup").json()
    assert body["enabled"] is False and body["state"] == "off"
    assert (body["day"], body["quota"], body["sent_today"]) == (1, 10, 0)
    assert body["ramp"][0] == 10 and set(body["presets"]) == {"gentle", "standard", "doubling"}


def test_put_saves_settings_and_ticks_the_scheduler(client):
    with patch("app.routers.warmup.run_scheduler_tick", new=AsyncMock()) as tick:
        r = client.put("/api/warmup", json={
            "enabled": True, "timezone": "Asia/Dhaka", "start_volume": 20,
            "growth_percent": 50, "target_volume": 300,
        })
    assert r.status_code == 200
    body = r.json()["warmup"]
    assert (body["enabled"], body["timezone"], body["quota"]) == (True, "Asia/Dhaka", 20)
    tick.assert_awaited_once()
    assert warmup.load_state()["target_volume"] == 300


@pytest.mark.parametrize("bad", [
    {"growth_percent": "20"}, {"enabled": "yes"}, {"junk": 1}, {"timezone": "Nope/Zone"},
    {"start_volume": 100, "target_volume": 50}, {},
])
def test_put_rejects_bad_input_with_a_400_and_writes_nothing(client, bad):
    with patch("app.routers.warmup.run_scheduler_tick", new=AsyncMock()) as tick:
        r = client.put("/api/warmup", json=bad)
    assert r.status_code == 400, r.text
    tick.assert_not_awaited()
    assert warmup.load_state()["start_volume"] == 10


def test_put_reports_a_write_failure_as_500(client):
    with patch("app.routers.warmup.warmup.update_settings", new=AsyncMock(side_effect=OSError("disk"))):
        r = client.put("/api/warmup", json={"enabled": True})
    assert r.status_code == 500


def test_preview_returns_the_ramp(client):
    r = client.get("/api/warmup/preview", params={"start_volume": 10, "growth_percent": 100, "target_volume": 80})
    assert r.json() == {"ramp": [10, 20, 40, 80], "days": 4, "total": 150}


def test_preview_validates_its_bounds(client):
    assert client.get("/api/warmup/preview", params={"start_volume": 100, "target_volume": 50}).status_code == 400
    assert client.get("/api/warmup/preview", params={"growth_percent": 9999}).status_code == 422
    assert client.get("/api/warmup/preview", params={"start_volume": 0}).status_code == 422


class _InsightsListMonk:
    def __init__(self, campaigns, settings, fail=False):
        self._campaigns, self._settings, self._fail = campaigns, settings, fail

    async def get_campaigns(self, **kw):
        raise AssertionError

    async def paginate_all(self, fn, per_page=100, **kw):
        if self._fail:
            raise httpx.ConnectError("down")
        return [c for c in self._campaigns if c["status"] == kw["status"]]

    async def get_settings(self):
        if self._fail:
            raise httpx.ConnectError("down")
        return {"data": self._settings}


def test_insights_forecast_the_queue_and_flag_a_fast_sender(client):
    stub = _InsightsListMonk(
        [{"status": "running", "to_send": 500, "sent": 100},
         {"status": "paused", "to_send": 50, "sent": 50}],
        {"app.message_rate": 10, "app.concurrency": 2},
    )
    with patch("app.routers.warmup.listmonk", stub):
        body = client.get("/api/warmup/insights").json()
    assert body["backlog"] == 400
    assert body["days_to_finish"] == warmup.estimate_days(warmup.load_state(), 400)
    assert (body["message_rate"], body["concurrency"], body["max_per_second"]) == (10, 2, 20)
    assert body["overshoot_warning"] is True, "20 msg/s against a 10-email day"


def test_insights_stay_quiet_for_a_slow_sender(client):
    stub = _InsightsListMonk([], {"app.message_rate": 1, "app.concurrency": 1})
    with patch("app.routers.warmup.listmonk", stub):
        body = client.get("/api/warmup/insights").json()
    assert body["backlog"] == 0 and body["overshoot_warning"] is False


def test_insights_degrade_instead_of_failing_when_listmonk_is_down(client):
    with patch("app.routers.warmup.listmonk", _InsightsListMonk([], {}, fail=True)):
        r = client.get("/api/warmup/insights")
    assert r.status_code == 200
    assert r.json()["backlog"] is None and r.json()["max_per_second"] is None


def test_starting_a_campaign_wakes_the_quota_loop(client):
    with patch("app.routers.campaigns.listmonk.change_campaign_status", new=AsyncMock(return_value={"data": True})), \
         patch("app.routers.campaigns.nudge_warmup") as nudge:
        assert client.put("/api/campaigns/3/status", json={"status": "running"}).status_code == 200
        nudge.assert_called_once()
        nudge.reset_mock()
        assert client.put("/api/campaigns/3/status", json={"status": "paused"}).status_code == 200
        nudge.assert_not_called()
