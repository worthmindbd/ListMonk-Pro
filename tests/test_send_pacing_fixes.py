"""Regressions for the live sending incident of 2026-11-12.

* A campaign ListMonk called "running" sent nothing for hours: a deleted
  campaign's fetched batch was still being pushed through a 1-per-15-min
  sliding window, and ListMonk drains every campaign through one loop. The
  app now spots a running campaign whose count stays flat far longer than the
  window explains (``send_stats.progress`` / ``GET /api/sends/progress``),
  and the audit flags a batch larger than the window rate.
* "1 msg / 15 min" sent 1 every 7.5 min: ListMonk lets two messages through a
  window when the rate is 1 (``sending_window``, ``sliding_window_rate``).
* "6 sent today" next to a 0-sent campaign came from a deleted campaign:
  ``/api/sends/daily`` now names it.
* Hidden "ghost ink" filler in templates is flagged (``hidden_text``).
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.main import app
from app.routers import campaigns as campaigns_router
from app.services import send_stats
from app.services.deliverability_service import (
    apply_deliverability_preset, get_deliverability_checklist, hidden_text_blocks, sending_window,
)
from tests.conftest import CSRFTestClient

UTC = ZoneInfo("UTC")
T0 = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)

GHOST = (
    '<p>Hi {{ .Subscriber.FirstName }}</p>\n'
    '<div data-gi="1" style="color:transparent;font-size:0px;line-height:0px;max-height:0px;'
    'mso-hide:all;overflow:hidden;display:none;opacity:0;">\n'
    '{{- if .Subscriber.Attribs.ghost_ink -}}{{ .Subscriber.Attribs.ghost_ink }}{{- else -}}'
    + "There is a quiet rhythm to video that you only start to notice after hours. " * 6
    + '{{- end -}}\n</div>\n<p>Thanks</p>'
)


def _window(rate, duration="15m", on=True, batch=100):
    return {
        "app.message_sliding_window": on,
        "app.message_sliding_window_rate": rate,
        "app.message_sliding_window_duration": duration,
        "app.batch_size": batch,
    }


def _http_error(status):
    req = httpx.Request("GET", "http://listmonk/api/campaigns/6")
    return httpx.HTTPStatusError(str(status), request=req, response=httpx.Response(status, request=req))


# ── The window as ListMonk enforces it ──────────────────────────────────────

def test_rate_one_window_really_sends_two():
    assert sending_window(_window(1))["effective_rate"] == 2
    assert sending_window(_window(2, "30m"))["effective_rate"] == 2
    assert sending_window(_window(5))["effective_rate"] == 5
    off = sending_window(_window(1, on=False))
    assert off["active"] is False and off["effective_rate"] is None
    # ListMonk ignores windows of a second or less.
    assert sending_window(_window(1, "1s"))["active"] is False


async def _audit(settings, templates=None):
    lm = MagicMock()
    lm.get_settings = AsyncMock(return_value={"data": settings})
    if templates is not None:
        lm.get_templates = AsyncMock(return_value={"data": templates})
    res = await get_deliverability_checklist(lm)
    return {c["id"]: c for c in res["checks"]}, res


async def test_batch_larger_than_window_rate_fails_with_the_hold_time():
    checks, _ = await _audit(_window(1, "15m", batch=100))
    batch = checks["batch_size"]
    assert batch["status"] == "fail"
    # 98 messages left at 2 per 15 min = 49 windows.
    assert "12 h 15 min" in batch["description"]
    assert batch["recommended"].startswith("<= 1")


async def test_batch_within_window_rate_passes_and_slightly_over_warns():
    checks, _ = await _audit(_window(2, "30m", batch=2))
    assert checks["batch_size"]["status"] == "pass"
    checks, _ = await _audit(_window(2, "30m", batch=4))
    assert checks["batch_size"]["status"] == "warn"     # one extra window (30 min)


async def test_batch_rule_without_window_is_unchanged():
    checks, _ = await _audit(_window(1, on=False, batch=100))
    assert checks["batch_size"]["status"] == "pass"
    checks, _ = await _audit(_window(1, on=False, batch=300))
    assert checks["batch_size"]["status"] == "fail"


async def test_sliding_window_rate_check():
    checks, res = await _audit(_window(1, "15m", batch=1))
    assert checks["sliding_window_rate"]["status"] == "warn"
    assert "really 2" in checks["sliding_window_rate"]["current_value"]
    # The cadence reports what really leaves: 2 per 15 min = 192/day.
    assert res["raw_settings"]["send_cadence_per_day"] == 192
    checks, _ = await _audit(_window(2, "30m", batch=2))
    assert checks["sliding_window_rate"]["status"] == "pass"
    checks, _ = await _audit(_window(1, on=False))
    assert checks["sliding_window_rate"]["status"] == "info"


async def test_cold_preset_sets_batch_to_window_rate():
    lm = MagicMock()
    lm.get_settings = AsyncMock(return_value={"data": _window(2, "30m", batch=100)})
    lm.apply_settings = AsyncMock(return_value={"data": True})
    res = await apply_deliverability_preset(lm, "cold_email")
    assert res["applied"]["app.batch_size"] == 2
    # Without a window (or unreadable settings) the old 100 stays.
    lm.get_settings = AsyncMock(side_effect=RuntimeError("down"))
    res = await apply_deliverability_preset(lm, "cold_email")
    assert res["applied"]["app.batch_size"] == 100


# ── Hidden template text ────────────────────────────────────────────────────

def test_ghost_ink_is_detected():
    blocks = hidden_text_blocks(GHOST)
    assert len(blocks) == 1 and blocks[0] > 400


def test_short_preheader_and_visible_styles_are_not_flagged():
    preheader = '<span style="display:none;max-height:0;">{{ .Subscriber.Attribs.preview }} Quick note</span>'
    assert hidden_text_blocks(preheader) == []
    long_visible = '<div style="opacity:0.5;font-size:0.9em">' + "words " * 100 + "</div>"
    assert hidden_text_blocks(long_visible) == []
    assert hidden_text_blocks("") == []


def test_nested_hidden_markup_counts_once():
    body = ('<div style="display: none"><div><span>' + "padding text " * 40
            + "</span></div><div>more</div></div><p>visible</p>")
    assert len(hidden_text_blocks(body)) == 1


async def test_hidden_text_check_names_the_template():
    templates = [
        {"id": 6, "name": "sequence_2_template", "type": "campaign", "body": GHOST},
        {"id": 5, "name": "sequence_1_template", "type": "campaign", "body": "<p>{{ UnsubscribeURL }}</p>"},
    ]
    checks, res = await _audit(_window(2, "30m", batch=2), templates)
    assert checks["hidden_text"]["status"] == "fail"
    assert "sequence_2_template" in checks["hidden_text"]["current_value"]
    assert [h["id"] for h in res["raw_settings"]["hidden_text_templates"]] == [6]
    clean = [templates[1]]
    checks, _ = await _audit(_window(2, "30m", batch=2), clean)
    assert checks["hidden_text"]["status"] == "pass"


# ── Stall detection ─────────────────────────────────────────────────────────

def test_flat_running_campaign_is_stalled_after_threshold():
    send_stats.track_progress([{"id": 7, "sent": 0, "to_send": 1730}], T0)
    send_stats.track_progress([{"id": 7, "sent": 0, "to_send": 1730}], T0 + timedelta(minutes=30))
    view = send_stats.progress(35 * 60, T0 + timedelta(minutes=30))
    assert view["campaigns"][0]["stalled"] is False
    view = send_stats.progress(35 * 60, T0 + timedelta(minutes=90))
    row = view["campaigns"][0]
    assert row["stalled"] is True and row["idle_seconds"] == 90 * 60
    assert view["last_poll"] == (T0 + timedelta(minutes=30)).isoformat()


def test_progress_resets_on_a_send_and_forgets_stopped_campaigns():
    send_stats.track_progress([{"id": 7, "sent": 0, "to_send": 10}], T0)
    send_stats.track_progress([{"id": 7, "sent": 1, "to_send": 10}], T0 + timedelta(hours=2))
    assert send_stats.progress(60, T0 + timedelta(hours=2))["campaigns"][0]["idle_seconds"] == 0
    send_stats.track_progress([], T0 + timedelta(hours=3))
    assert send_stats.progress(60, T0 + timedelta(hours=3))["campaigns"] == []


def test_fully_sent_campaign_is_never_stalled():
    send_stats.track_progress([{"id": 3, "sent": 10, "to_send": 10}], T0)
    assert send_stats.progress(60, T0 + timedelta(hours=5))["campaigns"][0]["stalled"] is False


# ── API ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app, raise_server_exceptions=False)


def test_progress_endpoint_uses_window_threshold_and_names_stalled(client):
    now = datetime.now().astimezone()
    send_stats.track_progress([{"id": 7, "sent": 0, "to_send": 1730}], now - timedelta(hours=2))
    send_stats.track_progress([{"id": 7, "sent": 0, "to_send": 1730}], now)
    with patch("app.routers.sends.listmonk") as lm:
        lm.get_settings = AsyncMock(return_value={"data": _window(2, "30m", batch=2)})
        lm.get_campaign = AsyncMock(return_value={"data": {"id": 7, "name": "Split A"}})
        res = client.get("/api/sends/progress")
    body = res.json()
    assert res.status_code == 200
    assert body["threshold_seconds"] == 2 * 1800 + 300
    assert body["campaigns"][0]["stalled"] is True
    assert body["campaigns"][0]["name"] == "Split A"


def test_progress_endpoint_degrades_when_settings_unreadable(client):
    with patch("app.routers.sends.listmonk") as lm:
        lm.get_settings = AsyncMock(side_effect=RuntimeError("down"))
        res = client.get("/api/sends/progress")
    assert res.status_code == 200
    assert res.json()["threshold_seconds"] == 2 * 3600


def test_daily_names_deleted_campaigns(client):
    state = send_stats._default_state()
    today = send_stats.today_in("UTC")
    state.update(baselined=True, counting_since=today, tracking_since=today)
    state["days"][today] = {"total": 8, "campaigns": {"6": 6, "7": 2}}
    send_stats.save_state(state)

    async def get_campaign(cid):
        if cid == 6:
            raise _http_error(404)
        return {"data": {"id": cid, "name": "Split A"}}

    with patch("app.routers.sends.listmonk") as lm:
        lm.get_campaign = AsyncMock(side_effect=get_campaign)
        body = client.get("/api/sends/daily?days=1").json()
    by_id = {c["id"]: c for c in body["today_by_campaign"]}
    assert by_id[6] == {"id": 6, "sent": 6, "name": "Deleted campaign #6", "deleted": True}
    assert by_id[7]["name"] == "Split A" and by_id[7]["deleted"] is False
    assert body["sent_today_by_deleted"] == 6


def test_daily_survives_a_failed_name_lookup(client):
    state = send_stats._default_state()
    today = send_stats.today_in("UTC")
    state.update(baselined=True, counting_since=today, tracking_since=today)
    state["days"][today] = {"total": 1, "campaigns": {"9": 1}}
    send_stats.save_state(state)
    with patch("app.routers.sends.listmonk") as lm:
        lm.get_campaign = AsyncMock(side_effect=_http_error(502))
        body = client.get("/api/sends/daily?days=1").json()
    assert body["today_by_campaign"][0]["deleted"] is None
    assert body["sent_today_by_deleted"] == 0


def test_summary_fresh_skips_the_cache(client):
    calls = []

    async def paginate_all(*_a, **_k):
        calls.append(1)
        return [{"id": 1, "status": "running", "sent": len(calls)}]

    with patch("app.routers.campaigns.listmonk") as lm:
        lm.paginate_all = AsyncMock(side_effect=paginate_all)
        client.get("/api/campaigns/summary")
        client.get("/api/campaigns/summary")
        assert len(calls) == 1
        client.get("/api/campaigns/summary?fresh=true")
        assert len(calls) == 2
    campaigns_router.invalidate_summary_cache()
