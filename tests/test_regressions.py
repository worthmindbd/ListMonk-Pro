"""Regression tests for bugs found during the project review."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield TestClient(app)


# ── Route shadowing ──────────────────────────────────────────────────────────

def test_bulk_blocklist_route_reaches_bulk_handler(client):
    """PUT /api/subscribers/blocklist must not be captured by /{subscriber_id}."""
    with patch(
        "app.routers.subscribers.listmonk.blocklist_subscribers",
        new_callable=AsyncMock,
        return_value={"data": {"blocklisted": 2}},
    ) as mock_bulk:
        resp = client.put("/api/subscribers/blocklist", json={"ids": [1, 2]})
    assert resp.status_code == 200
    mock_bulk.assert_awaited_once_with([1, 2])


def test_static_put_routes_registered_before_parameterized():
    from app.routers import subscribers

    paths = [r.path for r in subscribers.router.routes if "PUT" in (r.methods or [])]
    assert paths.index("/blocklist") < paths.index("/{subscriber_id}")
    assert paths.index("/lists") < paths.index("/{subscriber_id}")


# ── get_stats timestamp handling ─────────────────────────────────────────────

def test_get_stats_handles_naive_timestamps(tmp_path, monkeypatch):
    import app.services.unsubscribe_log as shared_log
    from app.services.imap_unsubscribe import get_stats

    log_file = tmp_path / "log.json"
    recent_naive = datetime.now().replace(tzinfo=None).isoformat()
    log_file.write_text(json.dumps([
        {"email": "old@example.com", "timestamp": "2020-01-01T00:00:00", "source": "email"},
        {"email": "new@example.com", "timestamp": recent_naive, "source": "link"},
    ]))
    monkeypatch.setattr(shared_log, "LOG_FILE", log_file)

    stats = get_stats()
    assert stats["total"] == 2
    assert stats["this_week"] == 1
    assert stats["link_count"] == 1


# ── Campaign prune / reattribution safety ────────────────────────────────────

def test_prune_keeps_records_without_campaign():
    from app.services.imap_unsubscribe import _prune_existing_records

    records = [
        {"email": "a@x.com", "campaign_id": 1},
        {"email": "b@x.com", "campaign_id": 999},   # campaign deleted
        {"email": "c@x.com", "campaign_id": None},  # no matching campaign
    ]
    pruned = _prune_existing_records(records, {1, 2})
    assert [r["email"] for r in pruned] == ["a@x.com", "c@x.com"]


def test_reattribute_does_not_remove_when_campaign_list_incomplete():
    from app.services.imap_unsubscribe import _reattribute_existing_records

    records = [{
        "email": "a@x.com",
        "source": "email",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "lists_removed": [1],
        "campaign_id": 5,
    }]
    changed, removed = _reattribute_existing_records(
        records, campaigns_list=[], allow_removal=False,
    )
    assert (changed, removed) == (0, 0)
    assert not records[0].get("_remove")


# ── Persistent processed-email set ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_processed_emails_survive_log_deletion(tmp_path, monkeypatch):
    import app.services.unsubscribe_log as shared_log
    from app.services.unsubscribe_log import (
        load_processed_emails, mark_processed, unmark_processed, save_log,
    )

    monkeypatch.setattr(shared_log, "LOG_FILE", tmp_path / "log.json")
    monkeypatch.setattr(shared_log, "PROCESSED_FILE", tmp_path / "processed.json")
    monkeypatch.setattr(shared_log, "SETTINGS_FILE", tmp_path / "settings.json")

    save_log([{"email": "a@x.com", "source": "email"}])
    assert "a@x.com" in load_processed_emails()  # seeded from log

    await mark_processed(["b@x.com"])
    save_log([])  # simulate "Remove"/"Clear" deleting log records
    processed = load_processed_emails()
    assert {"a@x.com", "b@x.com"} <= processed

    await unmark_processed(["a@x.com"])
    assert "a@x.com" not in load_processed_emails()


@pytest.mark.asyncio
async def test_link_scan_skips_log_deleted_but_processed_email(tmp_path, monkeypatch):
    """Deleting a log record must not make the scanner re-handle the email."""
    import app.services.unsubscribe_log as shared_log
    from app.services import link_unsubscribe as svc

    processed_file = tmp_path / "processed.json"
    processed_file.write_text(json.dumps(["gone@example.com"]))
    monkeypatch.setattr(shared_log, "LOG_FILE", tmp_path / "log.json")
    monkeypatch.setattr(shared_log, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(shared_log, "PROCESSED_FILE", processed_file)

    async def mock_request(method, path, **kwargs):
        if method == "GET" and path == "/api/lists":
            return {"data": {"results": [{"id": 1, "name": "N"}], "total": 1}}
        if method == "GET" and "/api/subscribers" in path:
            return {"data": {"results": [{
                "id": 5, "email": "gone@example.com", "name": "",
                "lists": [{"id": 1}],
            }], "total": 1}}
        if method == "GET" and "/api/campaigns" in path:
            return {"data": {"results": [], "total": 0}}
        return {"data": {}}

    from app.services.listmonk_client import ListMonkClient
    client = ListMonkClient.__new__(ListMonkClient)
    client._client = None
    client._request = mock_request

    result = await svc.scan_link_unsubscribes(client)
    assert result["processed"] == 0
    assert result["new_found"] == 0


# ── IMAP scan: messages are marked seen, undo sticks ─────────────────────────

class _FakeIMAP:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.flags: dict[str, str] = {}

    def select(self, mailbox):
        return ("OK", [b"1"])

    def search(self, charset, criteria):
        assert "UNSEEN" in criteria
        if self.flags:
            return ("OK", [b""])
        return ("OK", [b"1"])

    def fetch(self, msg_id, spec):
        return ("OK", [(b"1 (RFC822)", self.raw)])

    def store(self, msg_id, op, flags):
        self.flags[msg_id] = flags
        return ("OK", [b""])

    def logout(self):
        return ("BYE", [b""])


@pytest.mark.asyncio
async def test_imap_scan_marks_messages_seen_and_undo_sticks(tmp_path, monkeypatch):
    import email.message
    import app.services.unsubscribe_log as shared_log
    from app.services import imap_unsubscribe as svc

    monkeypatch.setattr(shared_log, "LOG_FILE", tmp_path / "log.json")
    monkeypatch.setattr(shared_log, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(shared_log, "PROCESSED_FILE", tmp_path / "processed.json")

    msg = email.message.EmailMessage()
    msg["From"] = "user@example.com"
    msg["To"] = "inbox@example.com"
    msg["Subject"] = "Re: Newsletter"
    msg["Message-ID"] = "<abc@example.com>"
    msg["Date"] = "Mon, 01 Sep 2025 12:00:00 +0000"
    msg.set_content("Please remove me from this list.")

    fake_conn = _FakeIMAP(msg.as_bytes())
    monkeypatch.setattr(svc, "connect_imap", lambda: fake_conn)

    modify_calls = []

    async def mock_request(method, path, **kwargs):
        if method == "GET" and "/api/campaigns" in path:
            return {"data": {"results": [{
                "id": 10, "name": "September", "created_at": "2025-09-01",
                "lists": [{"id": 1}],
            }], "total": 1}}
        if method == "GET" and "/api/subscribers" in path:
            return {"data": {"results": [{
                "id": 5, "email": "user@example.com", "name": "User",
                "lists": [{"id": 1}],
            }], "total": 1}}
        if method == "PUT" and "/api/subscribers/lists" in path:
            modify_calls.append(path)
            return {"data": {}}
        return {"data": {}}

    from app.services.listmonk_client import ListMonkClient
    client = ListMonkClient.__new__(ListMonkClient)
    client._client = None
    client._request = mock_request

    first = await svc.scan_and_unsubscribe(client)
    assert first["processed"] == 1
    assert fake_conn.flags.get(b"1") == "\\Seen"
    assert len(json.loads((tmp_path / "log.json").read_text())) == 1
    assert "user@example.com" in json.loads((tmp_path / "processed.json").read_text())

    # Simulate "Undo All": records + processed emails removed (as reset does).
    shared_log.save_log([])
    await shared_log.unmark_processed(["user@example.com"])

    second = await svc.scan_and_unsubscribe(client)
    assert second["processed"] == 0
    assert second["scanned"] == 0
    assert len(modify_calls) == 1, "Message must not be re-processed after undo"


# ── Security headers / preview sandbox / login throttling ────────────────────

def test_security_headers_present_on_login_page(client):
    resp = client.get("/auth/login")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("referrer-policy") == "same-origin"
    assert resp.headers.get("x-frame-options") == "SAMEORIGIN"


def test_campaign_preview_is_sandboxed(client):
    import types
    from unittest.mock import AsyncMock

    fake_resp = types.SimpleNamespace(text="<html><script>alert(1)</script></html>")
    with patch(
        "app.routers.campaigns.listmonk.preview_campaign",
        new_callable=AsyncMock,
        return_value=fake_resp,
    ):
        resp = client.get("/api/campaigns/1/preview")
    assert resp.status_code == 200
    assert resp.headers.get("content-security-policy") == "sandbox"
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_login_rate_limited_after_repeated_failures(client):
    import app.auth as auth

    ip = "testclient"
    auth.clear_login_failures(ip)
    try:
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            resp = client.post("/auth/login", json={"username": "x", "password": "bad"})
            assert resp.status_code == 401
        resp = client.post("/auth/login", json={"username": "x", "password": "bad"})
        assert resp.status_code == 429
    finally:
        auth.clear_login_failures(ip)


def test_login_rejects_malformed_json(client):
    resp = client.post(
        "/auth/login",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


# ── Scheduler validation ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_schedule_rejects_boolean_hour():
    from app.main import update_schedule

    with pytest.raises(HTTPException) as exc:
        await update_schedule({"start_hour": True})
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_update_unsub_settings_rejects_non_bool(monkeypatch):
    from app.routers.unsubscribes import update_unsub_settings
    from starlette.requests import Request

    scope = {
        "type": "http", "method": "PUT", "path": "/api/unsubscribes/settings",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
    }

    async def receive():
        return {"type": "http.request", "body": b'{"blocklist_enabled": "yes"}', "more_body": False}

    request = Request(scope, receive)
    with pytest.raises(HTTPException) as exc:
        await update_unsub_settings(request)
    assert exc.value.status_code == 400