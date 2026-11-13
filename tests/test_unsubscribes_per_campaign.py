"""Unsubscribes are browsed per campaign (Analytics), not on a page of their own.

The records no campaign view can show — never attributed, or their campaign
was deleted — are listed by ``GET /api/unsubscribes/orphans`` for the
Settings → Automation tab, so nothing in the log becomes unreachable.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.main import app
from app.services.listmonk_client import ListMonkClient
from tests.conftest import CSRFTestClient

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app, raise_server_exceptions=False)


@pytest.fixture
def unsub_log(tmp_path, monkeypatch):
    import app.services.unsubscribe_log as shared_log

    monkeypatch.setattr(shared_log, "LOG_FILE", tmp_path / "log.json")
    monkeypatch.setattr(shared_log, "PROCESSED_FILE", tmp_path / "p.json")
    monkeypatch.setattr(shared_log, "SETTINGS_FILE", tmp_path / "s.json")
    shared_log.save_log([
        {"email": "a@x.com", "campaign_id": 1, "campaign_name": "Live",
         "campaign_key": "2026-10", "timestamp": "2026-10-01T00:00:00+00:00"},
        {"email": "b@x.com", "campaign_id": 2, "campaign_name": "Gone",
         "campaign_key": "2026-09", "timestamp": "2026-09-02T00:00:00+00:00"},
        {"email": "c@x.com", "campaign_id": 2, "campaign_name": "Gone",
         "campaign_key": "2026-09", "timestamp": "2026-09-03T00:00:00+00:00"},
        {"email": "d@x.com", "campaign_name": "No matching campaign",
         "campaign_key": "unknown", "timestamp": "2026-09-04T00:00:00+00:00"},
    ])
    return shared_log


def _lm(live_ids, per_page_seen=None):
    lm = AsyncMock()

    async def get_campaigns(page=1, per_page=50, **kw):
        if per_page_seen is not None:
            per_page_seen.append(per_page)
        rows = [{"id": i} for i in live_ids]
        start = (page - 1) * per_page
        return {"data": {"results": rows[start:start + per_page], "total": len(rows)}}

    lm.get_campaigns = get_campaigns
    lm.iter_pages = lambda fn, **kw: ListMonkClient.iter_pages(None, fn, **kw)
    return lm


def test_orphans_lists_unattributed_and_deleted_campaigns(client, unsub_log):
    with patch("app.routers.unsubscribes.listmonk", _lm([1, 7])):
        resp = client.get("/api/unsubscribes/orphans")
    assert resp.status_code == 200
    got = {(g["campaign_id"], g["reason"], g["count"]) for g in resp.json()["data"]}
    assert got == {(2, "deleted", 2), ("unknown", "unattributed", 1)}


def test_orphans_reads_every_campaign_page(client, unsub_log):
    # Campaign 2 sits on the second page of ListMonk's list: it is live.
    with patch("app.routers.unsubscribes.listmonk", _lm(list(range(3, 150)) + [1, 2])):
        resp = client.get("/api/unsubscribes/orphans")
    assert [g["reason"] for g in resp.json()["data"]] == ["unattributed"]


def test_orphans_never_guesses_when_listmonk_is_down(client, unsub_log):
    lm = _lm([])

    async def down(**kw):
        raise httpx.ConnectError("refused")

    lm.get_campaigns = down
    with patch("app.routers.unsubscribes.listmonk", lm):
        resp = client.get("/api/unsubscribes/orphans")
    # Not a list that calls every campaign deleted.
    assert resp.status_code == 502


def test_orphans_skips_listmonk_when_only_unattributed(client, unsub_log):
    unsub_log.save_log([{"email": "d@x.com", "timestamp": "2026-09-04T00:00:00+00:00"}])
    lm = _lm([])
    lm.iter_pages = AsyncMock(side_effect=AssertionError("must not page campaigns"))
    with patch("app.routers.unsubscribes.listmonk", lm):
        resp = client.get("/api/unsubscribes/orphans")
    assert resp.status_code == 200
    assert [g["reason"] for g in resp.json()["data"]] == ["unattributed"]


# ── Frontend wiring ─────────────────────────────────────────────────────────

def _read(rel):
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_sidebar_has_no_unsubscribes_page():
    assert 'href="#/unsubscribes"' not in _read("templates/index.html")


def test_old_unsubscribe_links_redirect():
    src = _read("static/js/unsubscribes.js")
    assert "#/analytics?campaign=${id}&section=unsubscribes" in src
    assert "#/settings?tab=automation" in src


def test_campaign_page_links_to_its_analytics_unsubscribes():
    src = _read("static/js/campaigns.js")
    assert "#/unsubscribes?" not in src
    assert "#/analytics?campaign=${c.id}&section=unsubscribes" in src


def test_analytics_and_settings_mount_the_panels():
    assert "Unsubscribes.mount('anUnsubs'" in _read("static/js/analytics.js")
    settings_js = _read("static/js/settings.js")
    assert "Unsubscribes.mountMonitor('settingsUnsubMonitor')" in settings_js
    assert "Unsubscribes.mountOrphans('settingsUnsubOrphans')" in settings_js
