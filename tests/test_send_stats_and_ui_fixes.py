"""Daily send statistics ("sent today") and the UI-review bug fixes.

* ``send_stats`` counts every send per calendar day, warm-up on or off, with
  the same delta method as the warm-up quota, and never charges a campaign's
  earlier history to the day counting starts.
* The unattributed unsubscribe group ("unknown") can be opened, exported,
  scoped and removed.
* ``PUT /api/subscribers/{id}/unblock`` re-enables without dropping lists.
* ``GET /api/templates/{id}/preview`` proxies ListMonk's rendered preview.
"""

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.main import app
from app.services import blocklist_registry, send_stats, unsubscribe_log, warmup
from app.services import campaign_scheduler as sched
from tests.conftest import CSRFTestClient
from tests.test_warmup import FakeListMonk

UTC = ZoneInfo("UTC")
D1 = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
D1_LATE = datetime(2026, 11, 4, 23, 30, tzinfo=UTC)
D2 = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


@pytest.fixture
def lm():
    return FakeListMonk()


@pytest.fixture(autouse=True)
def _fresh_sched_lock(monkeypatch):
    monkeypatch.setattr(sched, "_SCHED_LOCK", asyncio.Lock())


async def poll(lm, now, tz="UTC"):
    stats = await warmup.fetch_running(lm)
    return await send_stats.observe(lm, stats, tz, now)


# ── Counting ────────────────────────────────────────────────────────────────

async def test_first_poll_baselines_so_history_is_not_charged_to_today(lm):
    lm.add(1, "running", sent=500)
    assert await poll(lm, D1) == 0
    assert send_stats.daily(1, D1)["sent_today"] == 0
    lm.send(1, 7)
    assert await poll(lm, D1) == 7
    view = send_stats.daily(1, D1)
    assert view["sent_today"] == 7
    assert view["today_by_campaign"] == [{"id": 1, "sent": 7}]


async def test_counts_per_campaign_and_per_day(lm):
    lm.add(1, "running")
    lm.add(2, "running")
    await poll(lm, D1)
    lm.send(1, 3)
    lm.send(2, 5)
    await poll(lm, D1_LATE)
    lm.send(1, 4)
    await poll(lm, D2)
    view = send_stats.daily(2, D2)
    assert [(d["date"], d["total"], d["tracked"]) for d in view["days"]] == [
        ("2026-11-04", 8, True), ("2026-11-05", 4, True),
    ]
    assert view["period_total"] == 12
    assert view["today_by_campaign"] == [{"id": 1, "sent": 4}]


async def test_final_messages_of_a_campaign_that_stopped_are_counted(lm):
    lm.add(1, "running")
    await poll(lm, D1)
    lm.send(1, 2)
    lm.campaigns[1]["status"] = "finished"   # left the running list
    await poll(lm, D1)
    assert send_stats.daily(1, D1)["sent_today"] == 2


async def test_counting_works_while_warmup_is_off(lm):
    assert warmup.load_state()["enabled"] is False
    lm.add(1, "running")
    await sched.enforce_warmup(lm)              # baseline
    lm.send(1, 9)
    delay = await sched.enforce_warmup(lm)
    assert send_stats.daily(1)["sent_today"] == 9
    assert lm.status_calls == [], "no quota is enforced while warm-up is off"
    assert delay == sched.WARMUP_IDLE_POLL


async def test_a_stats_failure_never_blocks_quota_enforcement(lm, monkeypatch):
    state = warmup.load_state()
    state.update(enabled=True, timezone="UTC", start_volume=10, growth_percent=100,
                 target_volume=80, date=warmup.today_for({"timezone": "UTC"}), baselined=True)
    warmup.save_state(state)
    monkeypatch.setattr(send_stats, "observe", AsyncMock(side_effect=OSError("disk full")))
    lm.add(1, "running")
    lm.send(1, 10)
    await sched.enforce_warmup(lm)
    assert lm.status_calls == [(1, "paused")]


async def test_the_poll_is_shared_with_warmup(lm):
    lm.add(1, "running")
    await sched.enforce_warmup(lm)             # baseline costs one campaign listing per status
    lm.calls = 0
    await sched.enforce_warmup(lm)
    assert lm.calls == 1, "one running-stats request per iteration, not one per consumer"


async def test_idle_polls_do_not_rewrite_the_file(lm, monkeypatch):
    await poll(lm, D1)
    writes = []
    real = send_stats.save_state
    monkeypatch.setattr(send_stats, "save_state", lambda s: (writes.append(1), real(s)))
    await poll(lm, D1)
    await poll(lm, D1)
    assert writes == []


# ── History and the view ────────────────────────────────────────────────────

async def test_first_run_is_seeded_from_the_warmup_history(lm):
    state = warmup.load_state()
    state["history"] = [
        {"date": "2026-11-01", "day": 1, "quota": 10, "sent": 10, "advanced": True},
        {"date": "2026-11-03", "day": 2, "quota": 12, "sent": 12, "advanced": True},
    ]
    warmup.save_state(state)
    await poll(lm, D1)
    days = send_stats.daily(5, D1)["days"]
    assert [(d["date"], d["total"], d["tracked"]) for d in days] == [
        ("2026-10-31", 0, False),
        ("2026-11-01", 10, True),
        ("2026-11-02", 0, False),    # a gap in the warm-up history is unknown, not zero
        ("2026-11-03", 12, True),
        ("2026-11-04", 0, True),     # counted by send_stats itself from here on
    ]
    assert send_stats.daily(5, D1)["tracking_since"] == "2026-11-01"


def test_view_without_any_state_is_untracked():
    view = send_stats.daily(3, D1)
    assert view["sent_today"] == 0
    assert view["tracking_since"] is None
    assert all(d["tracked"] is False for d in view["days"])


def test_corrupt_state_falls_back_to_defaults():
    send_stats.SEND_STATS_FILE.write_text(json.dumps({
        "timezone": "Mars/Base", "days": {"nope": {"total": 3}, "2026-11-04": {"total": -1}},
        "seen": {"1": "x"}, "baselined": "yes",
    }), encoding="utf-8")
    state = send_stats.load_state()
    assert state["timezone"] == "UTC"
    assert state["days"] == {}
    assert state["seen"] == {}
    assert state["baselined"] is False


async def test_day_boundary_follows_the_given_timezone(lm):
    lm.add(1, "running")
    await poll(lm, D1_LATE, tz="Asia/Dhaka")    # 23:30 UTC = 05:30 next day in Dhaka
    lm.send(1, 1)
    await poll(lm, D1_LATE, tz="Asia/Dhaka")
    assert send_stats.load_state()["days"] == {
        "2026-11-05": {"total": 1, "campaigns": {"1": 1}},
    }


# ── API ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app, raise_server_exceptions=False)


def test_daily_endpoint_bounds_days(client):
    assert client.get("/api/sends/daily?days=0").status_code == 422
    assert client.get("/api/sends/daily?days=366").status_code == 422
    res = client.get("/api/sends/daily?days=7")
    assert res.status_code == 200
    assert len(res.json()["days"]) == 7


# ── Unattributed unsubscribe group ──────────────────────────────────────────

def _seed_unsub_log():
    unsubscribe_log.save_log([
        {"email": "a@x.com", "campaign_id": None, "campaign_name": "", "source": "email", "timestamp": "2026-11-01T00:00:00"},
        {"email": "b@x.com", "campaign_name": "", "source": "link", "timestamp": "2026-11-02T00:00:00"},
        {"email": "a@x.com", "campaign_id": 6, "campaign_name": "C6", "source": "email", "timestamp": "2026-11-03T00:00:00"},
    ])


def test_unknown_group_lists_records_without_a_campaign(client):
    _seed_unsub_log()
    res = client.get("/api/unsubscribes/campaign/unknown")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["total"] == 2
    assert data["campaign_id"] == "unknown"
    assert {r["email"] for r in data["results"]} == {"a@x.com", "b@x.com"}
    assert client.get("/api/unsubscribes/campaign/6").json()["data"]["total"] == 1


def test_unknown_group_export_and_bad_ids(client):
    _seed_unsub_log()
    assert client.get("/api/unsubscribes/campaign/unknown/export").status_code == 200
    assert client.get("/api/unsubscribes/campaign/abc").status_code == 422


def test_record_removal_is_scoped_to_the_unknown_group(client):
    _seed_unsub_log()
    res = client.delete("/api/unsubscribes/records?emails=a@x.com&campaign_id=unknown")
    assert res.json()["removed"] == 1
    left = unsubscribe_log.load_log()
    assert [(r["email"], r.get("campaign_id")) for r in left] == [("b@x.com", None), ("a@x.com", 6)]


def test_removing_the_unknown_group_keeps_campaign_records(client):
    _seed_unsub_log()
    res = client.delete("/api/unsubscribes/campaign/unknown")
    assert res.json()["removed"] == 2
    assert "unattributed" in res.json()["message"]
    assert [r["campaign_id"] for r in unsubscribe_log.load_log()] == [6]


# ── Unblock ─────────────────────────────────────────────────────────────────

def test_unblock_keeps_lists_and_attributes(client):
    sub = {"id": 5, "email": "x@y.com", "name": "X", "status": "blocklisted",
           "attribs": {"company": "Acme"}, "lists": [{"id": 1}, {"id": 3}]}
    blocklist_registry.mark([5])
    with patch("app.routers.subscribers.listmonk") as lm:
        lm.get_subscriber = AsyncMock(return_value={"data": sub})
        lm.update_subscriber = AsyncMock(return_value={"data": {**sub, "status": "enabled"}})
        res = client.put("/api/subscribers/5/unblock")
    assert res.status_code == 200
    lm.update_subscriber.assert_awaited_once_with(5, {
        "email": "x@y.com", "name": "X", "status": "enabled",
        "attribs": {"company": "Acme"}, "lists": [1, 3],
    })
    assert 5 not in blocklist_registry.load()


def test_unblock_refuses_a_subscriber_that_is_not_blocklisted(client):
    with patch("app.routers.subscribers.listmonk") as lm:
        lm.get_subscriber = AsyncMock(return_value={"data": {"id": 5, "status": "enabled"}})
        lm.update_subscriber = AsyncMock()
        res = client.put("/api/subscribers/5/unblock")
    assert res.status_code == 409
    lm.update_subscriber.assert_not_awaited()


# ── Template preview ────────────────────────────────────────────────────────

def test_template_preview_is_rendered_by_listmonk_and_sandboxed(client):
    rendered = httpx.Response(200, text="<p>Hello Sample Subscriber</p>")
    with patch("app.routers.templates.listmonk") as lm:
        lm.preview_template = AsyncMock(return_value=rendered)
        res = client.get("/api/templates/5/preview")
    assert res.status_code == 200
    assert "Sample Subscriber" in res.text
    assert res.headers["content-security-policy"] == "sandbox"
    lm.preview_template.assert_awaited_once_with(5)
