"""Tests for the bulk data-management endpoints (select rows → act once).

These pin down the contract the list pages rely on: a bulk call only touches
the IDs it was given, reports partial failures instead of swallowing them, and
is registered before the parameterized routes it sits next to (otherwise
Starlette int-parses the literal path segment and answers 422).
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def reply_log(tmp_path, monkeypatch):
    """Point the reply log at a temp file holding three records."""
    import app.services.reply_log as rl

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "reply_log.json")
    monkeypatch.setattr(rl, "PROCESSED_FILE", tmp_path / "reply_processed.json")
    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    rl.save_log([
        {"id": "rep_1", "from_email": "a@x.com", "status": "unread", "sentiment": "positive"},
        {"id": "rep_2", "from_email": "b@x.com", "status": "unread", "sentiment": "question"},
        {"id": "rep_3", "from_email": "c@x.com", "status": "lead", "sentiment": "negative"},
    ])
    return rl


# ── ID payload validation ────────────────────────────────────────────────────

def test_parse_ids_dedupes_and_normalizes():
    from app.services.bulk_ops import parse_ids

    assert parse_ids(["3", 3, "4"]) == [3, 4]
    assert parse_ids(None) == []
    assert parse_ids([]) == []


@pytest.mark.parametrize("payload", ["1,2", {"a": 1}, [0], [-5], ["abc"], [None]])
def test_parse_ids_rejects_junk(payload):
    from app.services.bulk_ops import parse_ids

    with pytest.raises(ValueError):
        parse_ids(payload)


def test_parse_ids_caps_the_selection():
    from app.services.bulk_ops import MAX_BULK_IDS, parse_ids

    with pytest.raises(ValueError):
        parse_ids(list(range(1, MAX_BULK_IDS + 3)))


# ── Replies ──────────────────────────────────────────────────────────────────

def test_bulk_reply_status_updates_only_the_selection(client, reply_log):
    resp = client.put("/api/replies/status",
                      json={"ids": ["rep_1", "rep_2"], "status": "archived"})
    assert resp.status_code == 200
    assert resp.json()["updated"] == 2

    statuses = {r["id"]: r["status"] for r in reply_log.load_log()}
    assert statuses == {"rep_1": "archived", "rep_2": "archived", "rep_3": "lead"}


def test_bulk_reply_status_rejects_unknown_status(client, reply_log):
    resp = client.put("/api/replies/status",
                      json={"ids": ["rep_1"], "status": "exploded"})
    assert resp.status_code == 400
    assert reply_log.load_log()[0]["status"] == "unread"


def test_bulk_reply_status_404s_on_stale_selection(client, reply_log):
    resp = client.put("/api/replies/status",
                      json={"ids": ["rep_gone"], "status": "lead"})
    assert resp.status_code == 404


def test_bulk_reply_delete_removes_only_the_selection(client, reply_log):
    resp = client.delete("/api/replies?ids=rep_1&ids=rep_3")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2
    assert [r["id"] for r in reply_log.load_log()] == ["rep_2"]


def test_bulk_reply_blocklist_resolves_every_sender(client, reply_log):
    lm = AsyncMock()
    lm.get_subscribers = AsyncMock(
        return_value={"data": {"results": [{"id": 7, "email": "a@x.com"}]}}
    )
    lm.blocklist_subscriber = AsyncMock(return_value={"data": True})

    with patch("app.routers.replies.listmonk", lm):
        resp = client.post("/api/replies/blocklist", json={"ids": ["rep_1", "rep_2"]})

    assert resp.status_code == 200
    assert resp.json()["blocklisted"] == 2
    assert lm.blocklist_subscriber.await_count == 2


def test_bulk_reply_blocklist_reports_partial_failures(client, reply_log):
    """A sender ListMonk cannot resolve must be counted, not silently dropped."""
    lm = AsyncMock()

    async def fake_lookup(page, per_page, query, list_id=None):
        if "b@x.com" in query:
            return {"data": {"results": []}}
        return {"data": {"results": [{"id": 7, "email": "a@x.com"}]}}

    lm.get_subscribers = AsyncMock(side_effect=fake_lookup)
    lm.blocklist_subscriber = AsyncMock(return_value={"data": True})

    with patch("app.routers.replies.listmonk", lm):
        resp = client.post("/api/replies/blocklist", json={"ids": ["rep_1", "rep_2"]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["blocklisted"] == 1
    assert body["errors"] == 1


# ── Campaigns ────────────────────────────────────────────────────────────────

def test_campaign_archive_route_is_not_parsed_as_a_campaign_id(client):
    """PUT /api/campaigns/archive must not hit /{campaign_id} (422)."""
    lm = AsyncMock()
    lm.archive_campaign = AsyncMock(return_value={"data": True})

    with patch("app.routers.campaigns.listmonk", lm):
        resp = client.put("/api/campaigns/archive", json={"ids": [1, 2]})

    assert resp.status_code == 200, resp.text
    assert resp.json()["archived"] == 2
    assert lm.archive_campaign.await_count == 2


def test_campaign_bulk_archive_rejects_bad_ids(client):
    lm = AsyncMock()
    with patch("app.routers.campaigns.listmonk", lm):
        resp = client.put("/api/campaigns/archive", json={"ids": ["nope"]})
    assert resp.status_code == 400
    assert lm.archive_campaign.await_count == 0


def test_campaign_bulk_delete_passes_every_id(client):
    lm = AsyncMock()
    lm.delete_campaigns = AsyncMock(return_value={"data": True})

    with patch("app.routers.campaigns.listmonk", lm):
        resp = client.delete("/api/campaigns?ids=3&ids=4")

    assert resp.status_code == 200
    assert lm.delete_campaigns.await_args.args[0] == [3, 4]


# ── Subscribers ──────────────────────────────────────────────────────────────

def test_subscriber_status_filter_is_folded_into_the_query(client):
    lm = AsyncMock()
    lm.get_subscribers = AsyncMock(return_value={"data": {"results": [], "total": 0}})

    with patch("app.routers.subscribers.listmonk", lm):
        resp = client.get("/api/subscribers",
                          params={"status": "blocklisted", "query": "name like 'a'"})

    assert resp.status_code == 200
    forwarded = lm.get_subscribers.await_args.args[2]
    assert forwarded == "(name like 'a') AND subscribers.status = 'blocklisted'"


def test_subscriber_status_filter_alone(client):
    lm = AsyncMock()
    lm.get_subscribers = AsyncMock(return_value={"data": {"results": [], "total": 0}})

    with patch("app.routers.subscribers.listmonk", lm):
        client.get("/api/subscribers", params={"status": "enabled"})

    assert lm.get_subscribers.await_args.args[2] == "subscribers.status = 'enabled'"


def test_subscriber_status_filter_rejects_unknown_status(client):
    """The status lands in a ListMonk query string, so it must be enumerated."""
    resp = client.get("/api/subscribers",
                      params={"status": "enabled' OR subscribers.id > 0 --"})
    assert resp.status_code == 400


# ── Bounces ──────────────────────────────────────────────────────────────────

def test_bounce_delete_with_ids_never_falls_back_to_delete_all(client):
    lm = AsyncMock()
    lm.delete_bounce = AsyncMock(return_value={"data": True})
    lm.delete_all_bounces = AsyncMock(return_value={"data": True})

    with patch("app.routers.bounces.listmonk", lm), \
         patch("app.routers.bounces.schedule_hard_bounce_update"):
        resp = client.delete("/api/bounces?ids=5&ids=6")

    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2
    assert lm.delete_bounce.await_count == 2
    lm.delete_all_bounces.assert_not_awaited()


# ── Lists & Templates ────────────────────────────────────────────────────────

def test_list_bulk_delete_reports_partial_failure(client):
    lm = AsyncMock()

    async def fake_delete(lid):
        if lid == 2:
            raise RuntimeError("listmonk said no")
        return {"data": True}

    lm.delete_list = AsyncMock(side_effect=fake_delete)

    with patch("app.routers.lists.listmonk", lm):
        resp = client.delete("/api/lists?ids=1&ids=2")

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == 1
    assert body["errors"] == 1
    assert body["failed"] == [{"id": 2, "error": "listmonk said no"}]


def test_template_bulk_delete(client):
    lm = AsyncMock()
    lm.delete_template = AsyncMock(return_value={"data": True})

    with patch("app.routers.templates.listmonk", lm):
        resp = client.delete("/api/templates?ids=4&ids=5")

    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2


# ── Unsubscribes ─────────────────────────────────────────────────────────────

def test_unsubscribe_bulk_campaign_delete(tmp_path, monkeypatch, client):
    import app.services.unsubscribe_log as shared_log

    monkeypatch.setattr(shared_log, "LOG_FILE", tmp_path / "log.json")
    monkeypatch.setattr(shared_log, "PROCESSED_FILE", tmp_path / "p.json")
    monkeypatch.setattr(shared_log, "SETTINGS_FILE", tmp_path / "s.json")
    shared_log.save_log([
        {"email": "a@x.com", "campaign_id": 1, "timestamp": "2026-01-01T00:00:00+00:00"},
        {"email": "b@x.com", "campaign_id": 2, "timestamp": "2026-01-02T00:00:00+00:00"},
        {"email": "c@x.com", "campaign_id": 3, "timestamp": "2026-01-03T00:00:00+00:00"},
    ])

    resp = client.delete("/api/unsubscribes/campaigns?campaign_ids=1&campaign_ids=2")

    assert resp.status_code == 200
    assert resp.json()["removed"] == 2
    remaining = json.loads((tmp_path / "log.json").read_text())
    assert [r["campaign_id"] for r in remaining] == [3]


def test_unsubscribe_bulk_campaign_delete_needs_ids(client):
    resp = client.delete("/api/unsubscribes/campaigns")
    assert resp.status_code == 400


# ── The list pages must actually offer bulk management ───────────────────────

@pytest.mark.parametrize("js_file,key", [
    ("subscribers.js", "subscribers"),
    ("lists.js", "lists"),
    ("campaigns.js", "campaigns"),
    ("replies.js", "replies"),
    ("unsubscribes.js", "unsub_campaigns"),
])
def test_list_pages_register_bulk_actions(js_file, key):
    src = (REPO_ROOT / "static" / "js" / js_file).read_text()
    assert f"App.selection.register('{key}'" in src
    assert f"App.selection.bar('{key}')" in src


@pytest.mark.parametrize("key", ["templates", "bounces"])
def test_template_and_bounce_pages_register_bulk_actions(key):
    src = (REPO_ROOT / "static" / "js" / "app.js").read_text()
    assert f"App.selection.register('{key}'" in src
    assert f"App.selection.bar('{key}')" in src