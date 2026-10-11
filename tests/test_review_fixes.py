"""Regression tests for the issues found in the full-codebase review.

Each test names the defect it pins down so a future refactor that reintroduces
the bug fails loudly rather than silently regressing.
"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
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


# ── 1. Non-ASCII credentials must be a 401, never a 500 ─────────────────────

@pytest.mark.parametrize("username,password", [
    ("üser", "pw"),
    ("admin", "pässwörd"),
    ("ユーザー", "pw"),
    ("", ""),
])
def test_login_with_non_ascii_credentials_returns_401_not_500(monkeypatch, username, password):
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASS", "secret")
    c = TestClient(app, raise_server_exceptions=False)
    resp = c.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 401, f"{username!r} returned {resp.status_code}, not 401"


def test_non_ascii_login_is_rate_limited(monkeypatch):
    """The 500 previously escaped record_login_failure, defeating the limiter."""
    import app.auth as auth

    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASS", "secret")
    c = TestClient(app, raise_server_exceptions=False)
    ip = "testclient"
    auth.clear_login_failures(ip)
    try:
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            assert c.post("/auth/login", json={"username": "ü", "password": "x"}).status_code == 401
        assert c.post("/auth/login", json={"username": "ü", "password": "x"}).status_code == 429
    finally:
        auth.clear_login_failures(ip)


def test_check_credentials_supports_non_ascii_configured_password(monkeypatch):
    from app.auth import check_credentials

    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASS", "pässwörd")
    assert check_credentials("admin", "pässwörd") is True
    assert check_credentials("admin", "wrong") is False
    assert check_credentials("nope", "pässwörd") is False


# ── 2. The session signing key must never be committed or baked into an image ──

@pytest.mark.parametrize("ignore_file", [".gitignore", ".dockerignore"])
def test_session_secret_key_is_ignored(ignore_file):
    content = (REPO_ROOT / ignore_file).read_text()
    assert "session_secret.key" in content, f"{ignore_file} does not exclude the session key"


def test_session_secret_key_is_git_ignored():
    result = subprocess.run(
        ["git", "check-ignore", "session_secret.key"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, "git would track session_secret.key"


# ── 3. ListMonk returns "campaign": null for subscriber-level bounces ─────────

def test_bounce_campaign_helpers_tolerate_null_campaign():
    from app.services.bounce_filters import bounce_campaign_id, bounce_campaign_name

    for record in ({"campaign": None}, {}):
        assert bounce_campaign_id(record) is None
        assert bounce_campaign_name(record) == ""


def test_bounce_campaign_helpers_handle_real_values():
    from app.services.bounce_filters import bounce_campaign_id, bounce_campaign_name

    b = {"campaign": {"id": 7, "name": "Sept"}}
    assert bounce_campaign_id(b) == 7
    assert bounce_campaign_name(b) == "Sept"


@pytest.mark.asyncio
async def test_bounce_page_survives_null_campaign_records():
    """A single unattributed bounce must not 500 the whole Bounces page."""
    from app.services.bounce_list import fetch_filtered_bounces_page

    data = {"data": {"results": [
        {"id": 1, "email": "a@x.com", "type": "hard", "campaign": None},
        {"id": 2, "email": "b@x.com", "type": "hard", "campaign": {"id": 7}},
    ], "total": 2}}
    lm = AsyncMock()
    lm.get_bounces = AsyncMock(return_value=data)
    lm.get_subscribers = AsyncMock(return_value={"data": {"total": 0, "results": []}})

    res = await fetch_filtered_bounces_page(lm, page=1, per_page=25, bounce_type="hard")
    assert [b["id"] for b in res["data"]["results"]] == [1, 2]


def test_hard_bounce_cache_counts_null_campaign_without_crashing():
    """hard_bounce_cache used to raise inside its try, silently keeping stale counts."""
    from app.services.bounce_filters import bounce_campaign_id

    counts = {}
    for b in [{"campaign": None}, {"campaign": {"id": 3}}]:
        cid = bounce_campaign_id(b)
        if cid:
            counts[cid] = counts.get(cid, 0) + 1
    assert counts == {3: 1}


# ── 4. page/per_page must be bounded, or one request scans everything ────────

@pytest.mark.parametrize("path", [
    "/api/bounces", "/api/campaigns", "/api/subscribers", "/api/lists",
    "/api/unsubscribes",
])
def test_oversized_per_page_is_rejected(client, path):
    assert client.get(f"{path}?per_page=100000&page=1").status_code == 422


@pytest.mark.parametrize("query", ["per_page=0", "per_page=-1", "page=0", "page=-5"])
def test_non_positive_pagination_is_rejected(client, query):
    assert client.get(f"/api/bounces?{query}").status_code == 422


@pytest.mark.asyncio
async def test_large_bounce_batch_uses_bulk_opener_fetch(monkeypatch):
    """Cold cache + many bounces must not fan out to one query per bounce."""
    from app.services import bounce_list as bl

    monkeypatch.setattr(bl, "LM_FETCH_SIZE", 100)
    calls = {"subscriber_queries": 0}

    async def fake_get_bounces(page=1, per_page=100, campaign_id=None, source="", bounce_type=""):
        start = (page - 1) * 100
        rows = [
            {"id": start + i, "email": f"u{start + i}@x.com",
             "type": "hard", "campaign": {"id": 7}}
            for i in range(100)
        ] if start < 1000 else []
        return {"data": {"results": rows, "total": 1000}}

    async def fake_get_subscribers(page=1, per_page=50, query="", list_id=None):
        calls["subscriber_queries"] += 1
        return {"data": {"results": [], "total": 0}}

    lm = AsyncMock()
    lm.get_bounces = AsyncMock(side_effect=fake_get_bounces)
    lm.get_subscribers = AsyncMock(side_effect=fake_get_subscribers)

    await bl.fetch_filtered_bounces_page(lm, page=1, per_page=100, bounce_type="hard")
    # One opener fetch per distinct campaign, not one per bounce.
    assert calls["subscriber_queries"] <= 2, (
        f"fanned out to {calls['subscriber_queries']} opener queries"
    )


# ── 5. Re-attribution must not run against a truncated campaign list ─────────

class _HistoryClient:
    """Serves a DESC campaign history of `total` campaigns, 20 days apart."""

    def __init__(self, total: int):
        now = datetime.now(timezone.utc)
        self.camps = [
            {"id": i, "created_at": (now - timedelta(days=i * 20)).isoformat(),
             "lists": [{"id": 1}]}
            for i in range(total)
        ]

    async def get_campaigns(self, page=1, per_page=100, order_by="", order=""):
        chunk = self.camps[(page - 1) * per_page: page * per_page]
        return {"data": {"results": chunk, "total": len(self.camps)}}


@pytest.mark.asyncio
async def test_history_reaching_the_end_reports_complete():
    """Walking to the end means both pruning and re-attribution are safe."""
    from app.services.imap_unsubscribe import _fetch_campaign_history

    now = datetime.now(timezone.utc)
    client = _HistoryClient(150)
    camps, complete_all, covers = await _fetch_campaign_history(client, now)
    assert complete_all is True
    assert covers is True
    assert len(camps) == 150


@pytest.mark.asyncio
async def test_history_page_cap_stops_the_walk():
    """A huge history must not make the hourly scan unbounded."""
    from app.services.imap_unsubscribe import (
        _fetch_campaign_history, MAX_CAMPAIGN_PAGES, CAMPAIGN_PAGE_SIZE,
    )

    now = datetime.now(timezone.utc)
    client = _HistoryClient(5000)
    _, complete_all, covers = await _fetch_campaign_history(client, now)
    assert complete_all is False, "must not claim completeness after a capped walk"
    assert len(client.camps) == 5000  # sanity: fixture really is that big
    assert covers is True, "page 1 of 5000 easily covers 'now'"


@pytest.mark.asyncio
async def test_history_unreachable_cutoff_blocks_reattribution():
    """The corruption bug: a truncated list silently reassigned old records."""
    from app.services.imap_unsubscribe import (
        _fetch_campaign_history, MAX_CAMPAIGN_PAGES,
    )

    now = datetime.now(timezone.utc)
    client = _HistoryClient(5000)
    _, complete_all, covers = await _fetch_campaign_history(
        client, now - timedelta(days=50_000)
    )
    assert complete_all is False
    assert covers is False, "must refuse to re-attribute against a truncated window"


@pytest.mark.asyncio
async def test_history_steady_state_is_single_page_and_cheap():
    from app.services.imap_unsubscribe import _fetch_campaign_history

    client = _HistoryClient(150)
    camps, complete_all, covers = await _fetch_campaign_history(client, None)
    assert len(camps) == 100, "no pending records must not walk the whole history"
    assert covers is True
    assert complete_all is False


def test_reattribute_requires_complete_campaign_list():
    """A truncated list must never rewrite an existing attribution."""
    from app.services.imap_unsubscribe import _reattribute_existing_records

    records = [{
        "email": "old@x.com", "source": "email",
        "timestamp": "2020-01-01T00:00:00+00:00",
        "lists_removed": [1], "campaign_id": 5,
    }]
    campaigns = [{"id": 9, "name": "Recent", "created_at": "2026-01-01T00:00:00+00:00",
                  "lists": [{"id": 1}]}]
    _reattribute_existing_records(records, campaigns, allow_removal=False)
    assert records[0]["campaign_id"] == 5, "attribution was rewritten from a partial list"


# ── 6. An empty day set must be rejected, not silently stall all sending ──────

@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"days": []},
    {"enabled": "yes"},
    {"enabled": 1},
    {"days": "mon"},
    {"days": ["funday"]},
])
async def test_update_schedule_rejects_invalid_payloads(payload):
    from app.main import update_schedule

    with pytest.raises(HTTPException) as exc:
        await update_schedule(dict(payload))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_empty_days_does_not_pause_every_campaign():
    """A corrupt schedule file must not stall all sending forever."""
    from app.services import campaign_scheduler as sched

    corrupt = {"enabled": True, "timezone": "UTC", "start_hour": 8, "start_minute": 0,
               "end_hour": 20, "end_minute": 0, "days": [], "auto_paused_campaigns": []}
    calls = []

    class FakeClient:
        async def paginate_all(self, fn, per_page=100, **kw):
            calls.append(kw.get("status"))
            return [{"id": 1, "name": "X", "status": "running"}] if kw.get("status") == "running" else []

        async def change_campaign_status(self, cid, status):
            calls.append(("change", cid, status))

    with patch.object(sched, "load_schedule", return_value=dict(corrupt)), \
         patch.object(sched, "save_schedule"):
        await sched.run_scheduler_tick(FakeClient())

    assert calls == [], f"scheduler acted on an invalid schedule: {calls}"


# ── 7. Rate limiting must use the real client IP behind a proxy ───────────────

def _request_with(peer: str, xff: str | None = None):
    from starlette.requests import Request

    headers = [(b"host", b"testserver")]
    if xff:
        headers.append((b"x-forwarded-for", xff.encode()))
    return Request({
        "type": "http", "method": "POST", "path": "/auth/login",
        "headers": headers, "client": (peer, 1234),
    })


def test_forwarded_for_ignored_from_untrusted_public_peer(monkeypatch):
    """Otherwise an attacker spoofs a fresh IP per attempt and defeats the limit."""
    from app.auth import client_ip_from_request

    monkeypatch.delenv("TRUST_PROXY", raising=False)
    assert client_ip_from_request(_request_with("8.8.8.8", "203.0.113.9")) == "8.8.8.8"


def test_forwarded_for_honoured_from_loopback_proxy(monkeypatch):
    from app.auth import client_ip_from_request

    monkeypatch.delenv("TRUST_PROXY", raising=False)
    got = client_ip_from_request(_request_with("127.0.0.1", "203.0.113.9, 10.0.0.5"))
    assert got == "203.0.113.9", "left-most XFF entry is the original client"


def test_trust_proxy_opt_in(monkeypatch):
    from app.auth import client_ip_from_request

    monkeypatch.setenv("TRUST_PROXY", "1")
    assert client_ip_from_request(_request_with("8.8.8.8", "203.0.113.9")) == "203.0.113.9"

    monkeypatch.setenv("TRUST_PROXY", "0")
    assert client_ip_from_request(_request_with("127.0.0.1", "203.0.113.9")) == "127.0.0.1"


def test_missing_forwarded_for_falls_back_to_peer(monkeypatch):
    from app.auth import client_ip_from_request

    monkeypatch.delenv("TRUST_PROXY", raising=False)
    assert client_ip_from_request(_request_with("10.0.0.5")) == "10.0.0.5"


# ── 8. "Undo all" must be bounded and concurrent ─────────────────────────────

@pytest.mark.asyncio
async def test_reset_restores_every_record_concurrently(tmp_path, monkeypatch):
    import app.services.unsubscribe_log as shared_log
    from app.routers import unsubscribes as U

    monkeypatch.setattr(shared_log, "LOG_FILE", tmp_path / "log.json")
    monkeypatch.setattr(shared_log, "PROCESSED_FILE", tmp_path / "p.json")
    monkeypatch.setattr(shared_log, "SETTINGS_FILE", tmp_path / "s.json")
    shared_log.save_log([
        {"email": f"u{i}@x.com", "subscriber_id": i, "lists_removed": [1]}
        for i in range(1, 101)
    ])

    inflight = 0
    peak = 0

    class FakeLM:
        async def get_subscriber(self, sid):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0)
            inflight -= 1
            return {"data": {"id": sid, "email": f"u{sid}@x.com", "name": "",
                             "lists": [{"id": 1}], "attribs": {}}}

        async def update_subscriber(self, sid, data):
            await asyncio.sleep(0)

        async def modify_list_memberships(self, data):
            await asyncio.sleep(0)

    monkeypatch.setattr(U, "listmonk", FakeLM())
    result = await U.reset_all_unsubscribes()

    assert result["restored"] == 100
    assert result["failed"] == 0
    assert peak > 1, "records were still processed serially"
    assert peak <= U.RESET_CONCURRENCY


@pytest.mark.asyncio
async def test_reset_refuses_oversized_log(tmp_path, monkeypatch):
    import app.services.unsubscribe_log as shared_log
    from app.routers import unsubscribes as U

    monkeypatch.setattr(shared_log, "LOG_FILE", tmp_path / "log.json")
    monkeypatch.setattr(shared_log, "PROCESSED_FILE", tmp_path / "p.json")
    monkeypatch.setattr(shared_log, "SETTINGS_FILE", tmp_path / "s.json")
    shared_log.save_log([
        {"email": f"u{i}@x.com", "subscriber_id": i, "lists_removed": [1]}
        for i in range(1, U.RESET_MAX_RECORDS + 2)
    ])

    with pytest.raises(HTTPException) as exc:
        await U.reset_all_unsubscribes()
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_reset_keeps_failed_records_in_the_log(tmp_path, monkeypatch):
    import app.services.unsubscribe_log as shared_log
    from app.routers import unsubscribes as U

    monkeypatch.setattr(shared_log, "LOG_FILE", tmp_path / "log.json")
    monkeypatch.setattr(shared_log, "PROCESSED_FILE", tmp_path / "p.json")
    monkeypatch.setattr(shared_log, "SETTINGS_FILE", tmp_path / "s.json")
    shared_log.save_log([
        {"email": "ok@x.com", "subscriber_id": 1, "lists_removed": [1]},
        {"email": "boom@x.com", "subscriber_id": 2, "lists_removed": [1]},
    ])

    class FakeLM:
        async def get_subscriber(self, sid):
            if sid == 2:
                raise RuntimeError("listmonk down")
            return {"data": {"id": sid, "email": "ok@x.com", "name": "",
                             "lists": [{"id": 1}], "attribs": {}}}

        async def update_subscriber(self, sid, data):
            pass

        async def modify_list_memberships(self, data):
            pass

    monkeypatch.setattr(U, "listmonk", FakeLM())
    result = await U.reset_all_unsubscribes()

    assert result["restored"] == 1 and result["failed"] == 1
    remaining = json.loads((tmp_path / "log.json").read_text())
    assert [r["email"] for r in remaining] == ["boom@x.com"]


# ── 9. Deleting many bounces must not fan out full-table scans ───────────────

@pytest.mark.asyncio
async def test_hard_bounce_refresh_is_coalesced(monkeypatch):
    from app.services import hard_bounce_cache as hbc

    paginations = []

    class FakeLM:
        _client = object()

        def get_bounces(self, **kw):
            return None

        async def paginate_all(self, fn, per_page=500, **kw):
            paginations.append(1)
            return []

    monkeypatch.setattr(hbc, "listmonk", FakeLM())
    for _ in range(50):
        hbc.schedule_hard_bounce_update(min_delay=0.01)
    await asyncio.sleep(0.2)

    assert len(paginations) == 1, f"50 deletes triggered {len(paginations)} full scans"


# ── 10/11. Link attribution: full history + deterministic list choice ─────────

def test_link_attribution_is_deterministic():
    from app.services.link_unsubscribe import _pick_campaign_for_list_ids

    now = datetime.now(timezone.utc)
    campaigns = [
        {"id": 1, "name": "A", "created_at": (now - timedelta(days=1)).isoformat(),
         "lists": [{"id": 3}, {"id": 2}]},
    ]
    ids = {1, 2, 3}
    picks = {_pick_campaign_for_list_ids(campaigns, ids)["matched_list_id"] for _ in range(50)}
    assert len(picks) == 1, f"matched_list_id varied across runs: {picks}"


def test_link_fallback_list_id_is_deterministic():
    from app.services.link_unsubscribe import _pick_campaign_for_list_ids

    now = datetime.now(timezone.utc)
    campaigns = [
        {"id": 9, "name": "NoLists", "created_at": (now - timedelta(days=1)).isoformat()},
    ]
    picks = {
        _pick_campaign_for_list_ids(campaigns, {7, 5, 9})["matched_list_id"]
        for _ in range(50)
    }
    assert picks == {min({7, 5, 9})}


@pytest.mark.asyncio
async def test_link_scan_walks_past_the_first_campaign_page(tmp_path, monkeypatch):
    """A single 50-row page used to mis-attribute old unsubscribes."""
    import app.services.unsubscribe_log as shared_log
    from app.services import link_unsubscribe as svc

    monkeypatch.setattr(shared_log, "LOG_FILE", tmp_path / "log.json")
    monkeypatch.setattr(shared_log, "PROCESSED_FILE", tmp_path / "p.json")
    monkeypatch.setattr(shared_log, "SETTINGS_FILE", tmp_path / "s.json")
    shared_log.save_log([])
    svc.invalidate_campaign_history()

    now = datetime.now(timezone.utc)
    # 120 campaigns, newest first. The whole first page targets a different
    # list, so the correct match is only on page 2 — the old single-page fetch
    # saw nothing and logged "No matching campaign".
    campaigns = [
        {"id": i, "name": f"Campaign {i}",
         "created_at": (now - timedelta(days=2 * i)).isoformat(),
         "lists": [{"id": 1} if i >= 100 else {"id": 9}]}
        for i in range(120)
    ]

    campaign_pages = []

    async def mock_request(method, path, **kwargs):
        if method == "GET" and path == "/api/lists":
            return {"data": {"results": [{"id": 1, "name": "N"}], "total": 1}}
        if method == "GET" and "/api/subscribers" in path:
            return {"data": {"results": [
                {"id": 5, "email": "old@x.com", "name": "", "lists": [{"id": 1}]}
            ], "total": 1}}
        if method == "GET" and "/api/campaigns" in path:
            page = kwargs.get("params", {}).get("page", 1)
            campaign_pages.append(page)
            chunk = campaigns[(page - 1) * 100: page * 100]
            return {"data": {"results": chunk, "total": len(campaigns)}}
        return {"data": {}}

    from app.services.listmonk_client import ListMonkClient
    client = ListMonkClient.__new__(ListMonkClient)
    client._client = None
    client._request = mock_request

    await svc.scan_link_unsubscribes(client)

    assert max(campaign_pages) >= 2, "campaign history was truncated to one page"
    log = json.loads((tmp_path / "log.json").read_text())
    assert log[0]["campaign_id"] == 100, (
        f"expected the newest list-1 campaign (100), got {log[0]['campaign_id']}"
    )


# ── 12. Exports must stream, not buffer the whole dataset ────────────────────

@pytest.mark.asyncio
async def test_streaming_csv_consumes_rows_lazily():
    import csv
    import io
    from app.services.export_service import aiter_dicts_to_csv

    consumed = 0

    async def rows():
        nonlocal consumed
        for i in range(5):
            consumed += 1
            yield {"id": i, "email": f"u{i}@x.com", "lists": [{"id": 1}]}

    out = io.StringIO()
    async for chunk in aiter_dicts_to_csv(rows(), ["id", "email", "lists"]):
        out.write(chunk)
        # After the first chunk only the header exists — nothing buffered.
        if consumed == 0:
            assert list(csv.reader(io.StringIO(chunk))) == [["id", "email", "lists"]]

    parsed = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert len(parsed) == 5
    assert parsed[0]["lists"] == '[{"id": 1}]'


@pytest.mark.asyncio
async def test_iter_pages_stops_at_max_pages(monkeypatch):
    """An API that never returns an empty page must not loop forever."""
    from app.services.listmonk_client import ListMonkClient

    calls = {"n": 0}

    async def endless(page=1, per_page=100):
        calls["n"] += 1
        return {"data": {"results": [{"id": page}], "total": 10_000_000}}

    client = ListMonkClient.__new__(ListMonkClient)
    client._client = None
    items = await client.paginate_all(endless, per_page=100, max_pages=3)
    assert len(items) == 3
    assert calls["n"] == 3


def test_client_exports_still_return_csv(client):
    with patch(
        "app.routers.subscribers.listmonk.iter_pages"
    ) as fake:
        async def _rows():
            yield {"id": 1, "email": "a@x.com", "name": "A", "status": "enabled",
                   "created_at": "2026-01-01", "updated_at": "2026-01-01"}
        fake.side_effect = lambda *a, **k: _rows()
        resp = client.get("/api/subscribers/export-all")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    assert "a@x.com" in resp.text


# ── 15. Logout must not be a GET (forced-logout CSRF) + HSTS ─────────────────

def test_logout_requires_post(client):
    get_resp = client.get("/auth/logout", follow_redirects=False)
    assert "lmpro_session" not in get_resp.headers.get("set-cookie", "") or \
        "Max-Age=0" not in get_resp.headers.get("set-cookie", "")

    post_resp = client.post("/auth/logout", follow_redirects=False)
    assert "lmpro_session=" in post_resp.headers.get("set-cookie", "")


def test_logout_link_is_a_post_form():
    html = (REPO_ROOT / "templates" / "index.html").read_text()
    assert 'action="/auth/logout"' in html
    assert 'method="post"' in html.lower()
    assert 'href="/auth/logout"' not in html, "GET logout link is a forced-logout CSRF vector"


def test_hsts_sent_over_https_not_http(client):
    assert "strict-transport-security" not in client.get("/auth/login").headers
    https_resp = client.get("/auth/login", headers={"x-forwarded-proto": "https"})
    assert "max-age=31536000" in https_resp.headers.get("strict-transport-security", "")


# ── 20. The app must actually emit its own logs ─────────────────────────────

def test_logging_is_configured():
    result = subprocess.run(
        [sys.executable, "-c",
         "import app.main, logging; logging.getLogger('x').info('probe')"],
        cwd=REPO_ROOT, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert "probe" in result.stderr, f"logger.info was discarded:\n{result.stderr}"


# ── 22. email.utils must be imported explicitly, not as a side effect ────────

def test_email_utils_import_is_explicit():
    result = subprocess.run(
        [sys.executable, "-c",
         "import email; assert not hasattr(email, 'utils'); import app.services.bounce_ingest; "
         "assert email.utils.parsedate_to_datetime"],
        cwd=REPO_ROOT, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert result.returncode == 0, result.stderr


# ── 23. Failed actions must return a real status code ───────────────────────

def test_scheduler_run_failure_returns_502(client):
    with patch(
        "app.main.run_scheduler_tick",
        new_callable=AsyncMock, side_effect=RuntimeError("listmonk down"),
    ):
        resp = client.post("/api/scheduler/run")
    assert resp.status_code == 502


def test_auto_unblock_run_failure_returns_502(client):
    with patch(
        "app.main.find_blocklisted_engaged",
        new_callable=AsyncMock, side_effect=RuntimeError("listmonk down"),
    ):
        resp = client.post("/api/auto-unblock/run")
    assert resp.status_code == 502


def test_unsubscribe_scan_failure_returns_502(client):
    with patch(
        "app.routers.unsubscribes.scan_and_unsubscribe",
        new_callable=AsyncMock, side_effect=RuntimeError("imap down"),
    ):
        resp = client.post("/api/unsubscribes/scan")
    assert resp.status_code == 502


def test_auto_unblock_status_degrades_without_500(client):
    """A status readout must not blank the whole Settings page."""
    with patch(
        "app.main.listmonk.get_subscribers",
        new_callable=AsyncMock, side_effect=RuntimeError("down"),
    ):
        resp = client.get("/api/auto-unblock/status")
    assert resp.status_code == 200
    assert "error" in resp.json()
