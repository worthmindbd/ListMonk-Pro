"""Regression tests for the router-hardening pass of the full-codebase review.

Pins the contracts the UI relies on:

- free-text subscriber search reaches ListMonk as a safely escaped ILIKE
  clause instead of raw SQL (plain words used to 400 with
  ``pq: column "info" does not exist``);
- the analytics path segment is whitelisted, so a %2F-encoded value can never
  be interpolated into another upstream ListMonk URL;
- engagement exports honour (and strictly validate) their date bounds;
- every bulk route validates ids through ``parse_ids`` (cap + >= 1);
- failed IMAP scans answer a real status code instead of 200 "ok" (rule 7);
- non-string login credentials are a counted 400, not a 500;
- converter ``list_ids`` junk is a 400, not a 500;
- settings endpoints persist only keys they own;
- and the first direct unit coverage of session cookie signing/verification
  (every router test patches auth out, so the crypto itself was untested).
"""

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield TestClient(app, raise_server_exceptions=False)


# ── Subscriber free-text search ──────────────────────────────────────────────

def test_search_builder_wraps_term_in_escaped_ilike():
    from app.routers.subscribers import _search_to_query

    q = _search_to_query("info@acme")
    assert "subscribers.email ILIKE '%info@acme%'" in q
    assert "subscribers.name ILIKE '%info@acme%'" in q


def test_search_builder_escapes_quotes_and_strips_wildcards():
    from app.routers.subscribers import _search_to_query

    q = _search_to_query("o'brien")
    assert "o''brien" in q
    assert q.count("'") % 2 == 0 or "''" in q  # quotes are doubled, never bare
    # LIKE wildcards and backslashes are stripped, so the term can only ever
    # be a literal substring.
    assert _search_to_query("100%") == _search_to_query("100")
    assert _search_to_query("a_b\\c") == _search_to_query("abc")


def test_search_builder_rejects_empty_and_pure_wildcard_terms():
    from app.routers.subscribers import _search_to_query

    assert _search_to_query("") == ""
    assert _search_to_query("   ") == ""
    assert _search_to_query("%%__\\\\") == ""


def test_compose_query_combines_passthrough_search_and_status():
    from app.routers.subscribers import _compose_query

    q = _compose_query("", "bob", "enabled")
    assert "ILIKE '%bob%'" in q
    assert "subscribers.status = 'enabled'" in q

    q2 = _compose_query("subscribers.id > 5", "bob", "")
    assert "(subscribers.id > 5) AND" in q2
    assert "ILIKE '%bob%'" in q2

    # Nothing supplied → no query at all.
    assert _compose_query("", "", "") == ""


def test_subscribers_endpoint_sends_search_as_ilike(client):
    with patch("app.routers.subscribers.listmonk") as lm:
        lm.get_subscribers = AsyncMock(
            return_value={"data": {"results": [], "total": 0}})
        resp = client.get("/api/subscribers", params={"search": "info@regen"})
        assert resp.status_code == 200
        sent_query = lm.get_subscribers.call_args[0][2]
        assert "subscribers.email ILIKE '%info@regen%'" in sent_query


# ── Analytics whitelist & date-bounded engagement exports ────────────────────

def test_analytics_type_is_whitelisted(client):
    with patch("app.routers.campaigns.listmonk") as lm:
        lm.get_campaign_analytics = AsyncMock(return_value={"data": []})
        assert client.get("/api/campaigns/analytics/views").status_code == 200

        # Any segment outside the whitelist must be rejected before it can be
        # interpolated into an upstream ListMonk URL. (A literal
        # `..%2F..%2Fsettings` cannot be tested through TestClient — httpx
        # normalizes dot-segments client-side — but the whitelist covers the
        # decoded value the ASGI layer would hand the route either way.)
        for hostile in ("bogus", "views%3Fx%3D1", "settings", "Views"):
            r = client.get(f"/api/campaigns/analytics/{hostile}")
            assert r.status_code == 400, hostile
            r2 = client.get(f"/api/campaigns/analytics/{hostile}/export")
            assert r2.status_code == 400, hostile

        # Only the legitimate call ever reached the upstream client.
        assert lm.get_campaign_analytics.call_count == 1


def test_engagement_query_adds_inclusive_date_bounds():
    from app.routers.campaigns import _engagement_query

    q = _engagement_query(7, "views", "2026-10-01", "2026-10-14")
    assert "campaign_views" in q
    assert "created_at >= '2026-10-01'" in q
    # Inclusive end date → bounded by the following midnight.
    assert "created_at < '2026-10-15'" in q

    q2 = _engagement_query(7, "clicks")
    assert "link_clicks" in q2 and "created_at" not in q2
    assert _engagement_query(7, "bounces") is None


def test_engagement_export_validates_date_format(client):
    for params in ({"from_date": "abc"},
                   {"to_date": "2026-13-99"},
                   {"from_date": "2026-10-01", "to_date": "10/14/2026"}):
        r = client.get("/api/campaigns/1/subscribers/views/export", params=params)
        assert r.status_code == 400, params


# ── Bulk id validation on every bulk route (rule 14) ─────────────────────────

def test_bulk_list_delete_validates_ids(client):
    with patch("app.routers.lists.listmonk") as lm:
        lm.delete_list = AsyncMock()
        assert client.delete("/api/lists", params={"ids": [0]}).status_code == 400
        assert client.delete(
            "/api/lists", params={"ids": list(range(1, 502))}).status_code == 400
        lm.delete_list.assert_not_called()


def test_bulk_template_delete_validates_ids(client):
    with patch("app.routers.templates.listmonk") as lm:
        lm.delete_template = AsyncMock()
        assert client.delete("/api/templates", params={"ids": [-3]}).status_code == 400
        lm.delete_template.assert_not_called()


def test_bulk_campaign_delete_validates_ids(client):
    with patch("app.routers.campaigns.listmonk") as lm:
        lm.delete_campaigns = AsyncMock()
        assert client.delete("/api/campaigns", params={"ids": [0]}).status_code == 400
        lm.delete_campaigns.assert_not_called()


def test_bulk_subscriber_delete_validates_ids(client):
    with patch("app.routers.subscribers.listmonk") as lm:
        lm.delete_subscribers = AsyncMock()
        assert client.delete("/api/subscribers", params={"ids": [-1]}).status_code == 400
        lm.delete_subscribers.assert_not_called()


def test_bulk_blocklist_rejects_junk_payloads(client):
    with patch("app.routers.subscribers.listmonk") as lm:
        lm.blocklist_subscribers = AsyncMock()
        assert client.put("/api/subscribers/blocklist",
                          json={"ids": "1,2"}).status_code == 400
        assert client.put("/api/subscribers/blocklist", json={}).status_code == 400
        assert client.put("/api/subscribers/blocklist",
                          json={"ids": [0]}).status_code == 400
        lm.blocklist_subscribers.assert_not_called()


# ── Scan endpoints must not report failure as success (rule 7) ───────────────

def test_reply_scan_error_returns_502(client):
    with patch("app.routers.replies.scan_and_track_replies",
               AsyncMock(return_value={"error": "IMAP server unreachable",
                                       "new_replies": 0})):
        r = client.post("/api/replies/scan")
        assert r.status_code == 502
        assert "IMAP server unreachable" in r.json()["detail"]


def test_reply_scan_in_progress_stays_200(client):
    with patch("app.routers.replies.scan_and_track_replies",
               AsyncMock(return_value={"error": None, "new_replies": 0,
                                       "message": "Reply scan already in progress"})):
        assert client.post("/api/replies/scan").status_code == 200


def _imap_failure_result():
    return {"scanned": 0, "matched": 0, "processed": 0, "errors": 1,
            "message": "IMAP not configured or connection failed"}


def test_unsub_scan_imap_failure_returns_502(client, monkeypatch):
    import app.routers.unsubscribes as ur

    # IMAP IS configured → the failure message means "connection failed".
    monkeypatch.setattr(ur.settings, "imap_host", "mail.example.com")
    monkeypatch.setattr(ur.settings, "imap_user", "me@example.com")
    monkeypatch.setattr(ur.settings, "imap_pass", "secret")
    with patch.object(ur, "scan_and_unsubscribe",
                      AsyncMock(return_value=_imap_failure_result())), \
         patch.object(ur, "scan_link_unsubscribes",
                      AsyncMock(return_value={"processed": 2})):
        r = client.post("/api/unsubscribes/scan")
        assert r.status_code == 502
        assert "processed 2" in r.json()["detail"]


def test_unsub_scan_not_configured_stays_200_but_says_so(client, monkeypatch):
    import app.routers.unsubscribes as ur

    monkeypatch.setattr(ur.settings, "imap_host", "")
    with patch.object(ur, "scan_and_unsubscribe",
                      AsyncMock(return_value=_imap_failure_result())), \
         patch.object(ur, "scan_link_unsubscribes",
                      AsyncMock(return_value={"processed": 1})):
        r = client.post("/api/unsubscribes/scan")
        assert r.status_code == 200
        assert "not configured" in r.json()["message"]
        assert r.json()["link"]["processed"] == 1


# ── Login hardening ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload", [
    {"username": 123, "password": "x"},
    {"username": "admin", "password": []},
    {"username": None, "password": None},
])
def test_login_rejects_non_string_credentials_as_400(payload):
    with patch("app.main.record_login_failure") as rec:
        c = TestClient(app, raise_server_exceptions=False)
        r = c.post("/auth/login", json=payload)
        assert r.status_code == 400
        # The attempt still counts against the rate limiter.
        rec.assert_called_once()


# ── Converter input validation ───────────────────────────────────────────────

@pytest.mark.parametrize("raw", ['["abc"]', '[{"id": 1}]', '[null]'])
def test_parse_list_ids_rejects_junk_with_400(raw):
    from app.routers.converter import _parse_list_ids

    with pytest.raises(HTTPException) as excinfo:
        _parse_list_ids(raw)
    assert excinfo.value.status_code == 400


def test_parse_list_ids_accepts_numbers():
    from app.routers.converter import _parse_list_ids

    assert _parse_list_ids('[1, 2, "3"]') == [1, 2, 3]


# ── Settings endpoints persist only keys they own ────────────────────────────

def test_unsub_settings_whitelist(client, tmp_path, monkeypatch):
    import app.services.unsubscribe_log as ul

    monkeypatch.setattr(ul, "SETTINGS_FILE", tmp_path / "unsubscribe_settings.json")
    r = client.put("/api/unsubscribes/settings",
                   json={"blocklist_enabled": True, "junk": "x" * 1000})
    assert r.status_code == 200
    saved = ul.load_settings()
    assert saved["blocklist_enabled"] is True
    assert "junk" not in saved


def test_reply_settings_whitelist(client, tmp_path, monkeypatch):
    import app.services.reply_log as rl

    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    r = client.put("/api/replies/settings",
                   json={"auto_tag_leads": False, "evil": {"big": "payload"}})
    assert r.status_code == 200
    saved = rl.load_settings()
    assert saved["auto_tag_leads"] is False
    assert "evil" not in saved


# ── Session cookie signing / verification (direct unit coverage) ─────────────

class _FakeRequest:
    def __init__(self, cookies):
        self.cookies = cookies


def _token(timestamp=None, signature=None):
    from app import auth

    ts = str(timestamp if timestamp is not None else int(time.time()))
    return f"{ts}:{signature if signature is not None else auth._sign(ts)}"


def test_verify_session_accepts_fresh_valid_token():
    from app import auth

    req = _FakeRequest({auth.COOKIE_NAME: _token()})
    assert auth.verify_session(req) is True


def test_verify_session_rejects_tampered_signature():
    from app import auth

    ts = str(int(time.time()))
    req = _FakeRequest({auth.COOKIE_NAME: f"{ts}:{'0' * 64}"})
    assert auth.verify_session(req) is False


def test_verify_session_rejects_replayed_old_timestamp():
    from app import auth

    # Correctly signed, but older than SESSION_MAX_AGE: the signature check
    # passes and the expiry check must catch it.
    old = int(time.time()) - auth.SESSION_MAX_AGE - 60
    req = _FakeRequest({auth.COOKIE_NAME: _token(timestamp=old)})
    assert auth.verify_session(req) is False


def test_verify_session_rejects_malformed_tokens():
    from app import auth

    for tok in ["", "noseparator", "abc:def", ":", None]:
        req = _FakeRequest({auth.COOKIE_NAME: tok} if tok is not None else {})
        assert auth.verify_session(req) is False, tok


def test_verify_session_rejects_non_numeric_timestamp_with_valid_signature():
    from app import auth

    # A correctly signed but non-numeric timestamp must fail the expiry
    # parse, not crash it.
    req = _FakeRequest({auth.COOKIE_NAME: f"notanumber:{auth._sign('notanumber')}"})
    assert auth.verify_session(req) is False


def test_create_session_sets_httponly_samesite_cookie():
    from app import auth
    from fastapi.responses import JSONResponse

    resp = JSONResponse({"ok": True})
    auth.create_session(resp, None)
    header = resp.headers.get("set-cookie", "")
    assert auth.COOKIE_NAME in header
    assert "HttpOnly" in header
    assert "SameSite=lax" in header.lower() or "samesite=lax" in header.lower()


# ── Supplement: exact query strings the endpoints hand ListMonk ──────────────

def test_search_endpoint_sends_exact_ilike_clause(client):
    with patch("app.routers.subscribers.listmonk") as lm:
        lm.get_subscribers = AsyncMock(
            return_value={"data": {"results": [], "total": 0}})
        r = client.get("/api/subscribers", params={"search": "info@regen"})
        assert r.status_code == 200
        assert lm.get_subscribers.call_args[0][2] == (
            "(subscribers.email ILIKE '%info@regen%' "
            "OR subscribers.name ILIKE '%info@regen%')"
        )


def test_search_neutralizes_injection_attempt(client):
    with patch("app.routers.subscribers.listmonk") as lm:
        lm.get_subscribers = AsyncMock(
            return_value={"data": {"results": [], "total": 0}})
        client.get("/api/subscribers",
                   params={"search": "'; DROP TABLE subscribers; --"})
        sent = lm.get_subscribers.call_args[0][2]
        # The term's quote is doubled, so it stays a string literal.
        assert "''; DROP TABLE subscribers; --" in sent


def test_search_composes_exactly_with_query_and_status(client):
    from app.routers.subscribers import _compose_query

    composed = _compose_query("name like 'a'", "x", "enabled")
    assert composed == (
        "((name like 'a') AND ((subscribers.email ILIKE '%x%' "
        "OR subscribers.name ILIKE '%x%'))) AND subscribers.status = 'enabled'"
    )
    # The legacy query+status composition stays byte-identical.
    assert _compose_query("name like 'a'", "", "blocklisted") == (
        "(name like 'a') AND subscribers.status = 'blocklisted'"
    )


def test_export_all_honours_search(client):
    captured = {}

    async def _rows():
        yield {"id": 1, "email": "info@regen.example", "name": "R",
               "status": "enabled", "created_at": "2026-01-01",
               "updated_at": "2026-01-01"}

    def fake_iter_pages(fn, per_page=100, max_pages=0, **kwargs):
        captured.update(kwargs)
        return _rows()

    with patch("app.routers.subscribers.listmonk") as lm:
        lm.iter_pages = fake_iter_pages
        r = client.get("/api/subscribers/export-all",
                       params={"search": "info@regen"})
    assert r.status_code == 200
    assert "info@regen.example" in r.text
    assert "ILIKE '%info@regen%'" in captured["query"]


# ── Supplement: analytics traversal at the handler, dates at the endpoint ────

@pytest.mark.asyncio
async def test_analytics_traversal_segment_is_400_at_the_handler():
    """The literal `..%2F..%2Fsettings` value from the review finding.

    httpx/TestClient normalise dot-segments before routing can see them, so
    the exact hostile value is asserted against the handlers directly — the
    whitelist must reject it however it arrives.
    """
    from app.routers.campaigns import (
        get_campaign_analytics, export_campaign_analytics,
    )

    for handler in (get_campaign_analytics, export_campaign_analytics):
        with pytest.raises(HTTPException) as excinfo:
            await handler("..%2F..%2Fsettings")
        assert excinfo.value.status_code == 400


def test_analytics_encoded_traversal_is_400_through_the_stack(client):
    """A %2F-encoded value decodes into extra path segments before routing;
    the analytics catch-all must reject it with an explicit 400 (never a
    bare 404, and never a call upstream)."""
    with patch("app.routers.campaigns.listmonk") as lm:
        lm.get_campaign_analytics = AsyncMock(return_value={"data": []})
        r = client.get("/api/campaigns/analytics/..%2F..%2Fsettings")
        assert r.status_code == 400
        lm.get_campaign_analytics.assert_not_awaited()


def test_engagement_export_forwards_date_predicate_to_listmonk(client):
    """The validated bounds must actually reach the ListMonk query."""
    captured = {}

    async def _rows():
        yield {"id": 1, "email": "a@x.com", "name": "A", "status": "enabled",
               "lists": [], "attribs": {}, "created_at": "2026-10-05"}

    def fake_iter_pages(fn, per_page=100, **kwargs):
        captured.update(kwargs)
        return _rows()

    with patch("app.routers.campaigns.listmonk") as lm:
        lm.iter_pages = fake_iter_pages
        resp = client.get(
            "/api/campaigns/7/subscribers/clicks/export",
            params={"from_date": "2026-10-01", "to_date": "2026-10-14"},
        )

    assert resp.status_code == 200
    assert "a@x.com" in resp.text
    assert "link_clicks" in captured["query"]
    assert "created_at >= '2026-10-01'" in captured["query"]
    assert "created_at < '2026-10-15'" in captured["query"]


def test_bounce_export_filters_rows_by_date_window(client):
    bounces = [
        {"email": "before@x.com", "type": "hard", "source": "api",
         "created_at": "2026-09-30T23:59:59+00:00"},
        {"email": "inside@x.com", "type": "hard", "source": "api",
         "created_at": "2026-10-05T08:00:00+00:00"},
        {"email": "after@x.com", "type": "hard", "source": "api",
         "created_at": "2026-10-15T00:00:01+00:00"},
    ]

    async def fake_iter(lm_client, bounce_type, campaign_id=None):
        for b in bounces:
            yield b

    with patch("app.routers.campaigns.iter_filtered_bounces", fake_iter), \
         patch("app.routers.campaigns.listmonk", AsyncMock()):
        resp = client.get(
            "/api/campaigns/7/subscribers/bounces/export",
            params={"from_date": "2026-10-01", "to_date": "2026-10-14"},
        )

    assert resp.status_code == 200
    assert "inside@x.com" in resp.text
    assert "before@x.com" not in resp.text
    assert "after@x.com" not in resp.text


# ── Supplement: unsubscribe scan per-message errors & success contract ───────

def test_unsub_scan_per_message_errors_return_502(client, monkeypatch):
    """A configured mailbox whose scan counted errors is a failed scan."""
    import app.routers.unsubscribes as ur

    monkeypatch.setattr(ur.settings, "imap_host", "mail.example.com")
    monkeypatch.setattr(ur.settings, "imap_user", "me@example.com")
    monkeypatch.setattr(ur.settings, "imap_pass", "secret")
    imap_result = {"scanned": 6, "matched": 3, "processed": 2, "errors": 1,
                   "message": "Scan complete: 2 unsubscribed"}
    with patch.object(ur, "scan_and_unsubscribe",
                      AsyncMock(return_value=imap_result)), \
         patch.object(ur, "scan_link_unsubscribes",
                      AsyncMock(return_value={"processed": 1})):
        r = client.post("/api/unsubscribes/scan")
        assert r.status_code == 502
        detail = r.json()["detail"]
        assert "1 error(s)" in detail
        assert "link scan processed 1" in detail


def test_unsub_scan_success_keeps_response_contract(client, monkeypatch):
    """The frontend reads `imap`, `link` and `message` — keep them intact."""
    import app.routers.unsubscribes as ur

    monkeypatch.setattr(ur.settings, "imap_host", "mail.example.com")
    monkeypatch.setattr(ur.settings, "imap_user", "me@example.com")
    monkeypatch.setattr(ur.settings, "imap_pass", "secret")
    imap_result = {"scanned": 6, "matched": 3, "processed": 2, "errors": 0,
                   "message": "Scan complete: 2 unsubscribed"}
    link_result = {"processed": 1}
    with patch.object(ur, "scan_and_unsubscribe",
                      AsyncMock(return_value=imap_result)), \
         patch.object(ur, "scan_link_unsubscribes",
                      AsyncMock(return_value=link_result)):
        r = client.post("/api/unsubscribes/scan")
        assert r.status_code == 200
        body = r.json()
        assert body["imap"] == imap_result
        assert body["link"] == link_result
        assert body["message"] == "Scan complete: 3 unsubscribed"


# ── Supplement: login limiter counts non-string attempts end-to-end ──────────

def test_login_limiter_trips_on_non_string_attempts(monkeypatch):
    import app.auth as auth

    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASS", "secret")
    c = TestClient(app, raise_server_exceptions=False)
    for _ in range(auth.LOGIN_MAX_ATTEMPTS):
        assert c.post("/auth/login",
                      json={"username": 123, "password": "x"}).status_code == 400
    # Limiter tripped: even valid string credentials are refused until the
    # window passes — the malformed attempts were counted, not skipped.
    assert c.post("/auth/login",
                  json={"username": "admin", "password": "secret"}).status_code == 429


# ── Supplement: converter upload streaming ───────────────────────────────────

@pytest.mark.asyncio
async def test_read_upload_aborts_oversized_stream_without_buffering_it(monkeypatch):
    """A chunked upload (no declared size) must 413 before being drained."""
    from app.routers import converter as conv

    class EndlessUpload:
        size = None  # chunked transfer: no declared size available

        def __init__(self):
            self.reads = 0

        async def read(self, n=-1):
            self.reads += 1
            return b"x" * (n if n > 0 else 65536)

    monkeypatch.setattr(conv, "MAX_UPLOAD_BYTES", 4096)
    upload = EndlessUpload()
    with pytest.raises(HTTPException) as excinfo:
        await conv._read_upload(upload)
    assert excinfo.value.status_code == 413
    # 64 KiB chunks against a 4 KiB cap: the very first read already exceeds
    # the limit, so the stream is aborted instead of fully buffered.
    assert upload.reads == 1


@pytest.mark.asyncio
async def test_read_upload_rejects_empty_file():
    from app.routers import converter as conv

    class EmptyUpload:
        size = 0

        async def read(self, n=-1):
            return b""

    with pytest.raises(HTTPException) as excinfo:
        await conv._read_upload(EmptyUpload())
    assert excinfo.value.status_code == 400


# ── Supplement: scheduler PUT goes through the locked service API ────────────

def test_scheduler_put_persists_via_locked_service_api(client, tmp_path, monkeypatch):
    import app.services.campaign_scheduler as sched

    monkeypatch.setattr(sched, "SCHEDULE_FILE", tmp_path / "schedule.json")
    with patch("app.main.run_scheduler_tick", AsyncMock()) as tick:
        r = client.put("/api/scheduler", json={
            "enabled": True, "start_hour": 9,
            "auto_paused_campaigns": [99],  # runtime state: must be ignored
        })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["schedule"]["enabled"] is True
    assert body["schedule"]["start_hour"] == 9
    assert body["schedule"]["auto_paused_campaigns"] == []
    tick.assert_awaited_once()  # enabled schedules re-tick immediately
    stored = json.loads((tmp_path / "schedule.json").read_text())
    assert stored["start_hour"] == 9
    assert stored["auto_paused_campaigns"] == []


def test_scheduler_put_write_failure_is_500(client, monkeypatch):
    import app.services.campaign_scheduler as sched

    def read_only(schedule):
        raise OSError("read-only file system")

    monkeypatch.setattr(sched, "save_schedule", read_only)
    r = client.put("/api/scheduler", json={"enabled": False})
    assert r.status_code == 500
    assert "schedule file" in r.json()["detail"].lower()


# ── Supplement: cache invalidation hook & bulk id dedupe ─────────────────────

def test_invalidate_summary_cache_clears_cache_and_timestamp():
    from app.routers import campaigns as C

    C._summary_cache = {"total_campaigns": 42}
    C._summary_cached_at = 123.0
    C.invalidate_summary_cache()
    assert C._summary_cache == {}
    assert C._summary_cached_at == 0.0


def test_conftest_resets_shared_state_between_tests():
    """The autouse reset must leave every rule-11 cache empty at test start."""
    from app import auth
    from app.routers import campaigns as C
    from app.services import hard_bounce_cache as hbc

    assert C._summary_cache == {}
    assert hbc._hard_bounce_counts == {}
    assert hbc._pending_update is None
    assert auth._login_failures == {}


def test_bulk_blocklist_dedupes_ids(client):
    with patch("app.routers.subscribers.listmonk") as lm:
        lm.blocklist_subscribers = AsyncMock(return_value={"data": True})
        r = client.put("/api/subscribers/blocklist", json={"ids": [3, "3", 4]})
        assert r.status_code == 200
        lm.blocklist_subscribers.assert_awaited_once_with([3, 4])


# ── Supplement: packaging pins ────────────────────────────────────────────────

def test_dockerignore_keeps_favicon_after_png_exclusion():
    """`*.png` used to strip static/favicon.png → /favicon.ico 500'd in prod."""
    lines = (REPO_ROOT / ".dockerignore").read_text().splitlines()
    assert "!static/favicon.png" in lines
    assert lines.index("!static/favicon.png") > lines.index("*.png")


def test_deploy_workflow_gates_on_tests():
    content = (REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    assert "needs: test" in content
    assert "pytest tests/ -q" in content
