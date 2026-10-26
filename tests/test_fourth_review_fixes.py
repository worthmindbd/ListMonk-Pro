"""Regressions for the fourth full-codebase review.

Each test pins one defect that shipped: bounce pagination totals, the
auto-unblock scope, two CSRF bypasses, the IMAP unsubscribe scanner's
reliance on \\Seen, unbounded/unsafe bulk payloads, and a set of smaller
correctness issues across the scanners, routers and converter.
"""

import email
import email.message
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.main import app
from tests.conftest import CSRFTestClient


@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app, raise_server_exceptions=False)


# ── Bounce listing: total must not be clamped when a page fills early ──────

@pytest.mark.asyncio
async def test_bounce_page_total_not_clamped_when_page_fills_early():
    """With >100 raw bounces, filling page 1 from the first ListMonk page
    used to look like "reached the end" (lm_page had already advanced), so
    the total collapsed to the 100 rows collected and pages 3+ vanished."""
    from app.services.bounce_list import fetch_filtered_bounces_page

    def _rows(start, n):
        return [{"id": i, "email": f"s{i}@x.com", "type": "soft", "campaign": {"id": 1}}
                for i in range(start, start + n)]

    async def get_bounces(page=1, per_page=100, campaign_id=None, source="", bounce_type=""):
        if page == 1:
            return {"data": {"results": _rows(1, 100), "total": 150}}
        if page == 2:
            return {"data": {"results": _rows(101, 50), "total": 150}}
        return {"data": {"results": [], "total": 150}}

    client = AsyncMock()
    client.get_bounces = AsyncMock(side_effect=get_bounces)

    res = await fetch_filtered_bounces_page(client, page=1, per_page=50, bounce_type="")
    assert res["data"]["total"] == 150
    assert len(res["data"]["results"]) == 50

    # The genuinely last page still reports the exact served count.
    res = await fetch_filtered_bounces_page(client, page=3, per_page=50, bounce_type="")
    assert res["data"]["total"] == 150
    assert [b["id"] for b in res["data"]["results"]][:1] == [101]


# ── Auto-unblock: only bounce-caused blocklists, never opt-outs ────────────

def test_auto_unblock_query_requires_a_bounce_record():
    from app.services.auto_unblock import QUERY_BLOCKLISTED_ENGAGED

    assert "FROM bounces" in QUERY_BLOCKLISTED_ENGAGED
    assert "subscribers.status = 'blocklisted'" in QUERY_BLOCKLISTED_ENGAGED


@pytest.mark.asyncio
async def test_auto_unblock_never_reenables_unsubscribed_addresses():
    """Someone who replied "remove me" opened the email — engagement alone
    used to re-enable them every six hours."""
    import app.services.unsubscribe_log as ul
    from app.services.auto_unblock import find_blocklisted_engaged

    ul.save_processed_emails({"optout@x.com"})
    ul.save_log([{"email": "Logged@X.com", "campaign_id": 1}])

    client = AsyncMock()
    client.paginate_all = AsyncMock(return_value=[
        {"id": 1, "email": "optout@x.com"},
        {"id": 2, "email": "logged@x.com"},
        {"id": 3, "email": "bounced@x.com"},
    ])
    subs = await find_blocklisted_engaged(client)
    assert [s["id"] for s in subs] == [3]


def test_auto_unblock_status_counts_the_filtered_candidates(client):
    with patch("app.main.find_blocklisted_engaged",
               new=AsyncMock(return_value=[{"id": 3}, {"id": 4}])):
        resp = client.get("/api/auto-unblock/status")
    assert resp.status_code == 200
    assert resp.json()["blocklisted_engaged"] == 2


# ── CSRF: multipart and content-type parameter bypasses ────────────────────

def test_csrf_rejects_multipart_without_xrw():
    """A plain HTML form can submit multipart/form-data without a preflight."""
    from starlette.testclient import TestClient

    with patch("app.main.verify_session", return_value=True):
        c = TestClient(app, raise_server_exceptions=False)
        r = c.post("/api/unsubscribes/reset",
                   files={"x": ("x.txt", b"x", "text/plain")})
    assert r.status_code == 403


def test_csrf_rejects_json_smuggled_as_a_content_type_parameter():
    """A no-cors fetch may send "text/plain; x=application/json"."""
    from starlette.testclient import TestClient

    with patch("app.main.verify_session", return_value=True):
        c = TestClient(app, raise_server_exceptions=False)
        r = c.post("/api/auto-unblock/run", content=b"{}",
                   headers={"Content-Type": "text/plain; x=application/json"})
    assert r.status_code == 403


def test_csrf_still_accepts_spa_uploads(client):
    """The SPA's uploads carry X-Requested-With and must keep working."""
    r = client.post("/api/converter/detect-columns",
                    files={"file": ("t.csv", b"email\na@b.co\n", "text/csv")})
    assert r.status_code == 200


def test_csp_allows_the_google_fonts_stylesheet(client):
    r = client.get("/health")
    csp = r.headers["content-security-policy"]
    style_src = next(part for part in csp.split(";") if "style-src" in part)
    assert "https://fonts.googleapis.com" in style_src


# ── IMAP unsubscribe scanner: \Seen is never "processed" ───────────────────

def _mail(sender, body, message_id):
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = "inbox@example.com"
    msg["Subject"] = "Re: October"
    msg["Message-ID"] = message_id
    msg["Date"] = "Mon, 05 Oct 2026 12:00:00 +0000"
    msg.set_content(body)
    return msg.as_bytes()


class _Inbox:
    """IMAP double: every message is (already) read by the human."""

    def __init__(self, raws, seen=()):
        self.raws = raws
        self.seen = list(seen)      # messages already flagged \Seen
        self.criteria = []
        self.specs = []
        self.stored = []

    def select(self, mailbox):
        return ("OK", [b"1"])

    def search(self, charset, criteria):
        self.criteria.append(criteria)
        if criteria.startswith("(SEEN "):
            return ("OK", [b" ".join(self.seen)])
        return ("OK", [b" ".join(str(i + 1).encode() for i in range(len(self.raws)))])

    def fetch(self, num, spec):
        self.specs.append((num, spec))
        if b"," in num:
            # Batched header peek: answer per sequence number like a server.
            out = []
            for n in num.split(b","):
                out.append((n + b" (BODY[HEADER.FIELDS (MESSAGE-ID)] {1}", self.raws[int(n) - 1]))
                out.append(b")")
            return ("OK", out)
        return ("OK", [(num + b" (BODY[] {1}", self.raws[int(num) - 1])])

    def store(self, num, op, flags):
        self.stored.append(num)
        return ("OK", [b""])

    def logout(self):
        return ("BYE", [b""])


def _unsub_client(fail_lookup=None):
    modify_calls = []

    async def mock_request(method, path, **kwargs):
        if method == "GET" and "/api/campaigns" in path:
            return {"data": {"results": [{
                "id": 10, "name": "October", "created_at": "2026-10-01",
                "lists": [{"id": 1}],
            }], "total": 1}}
        if method == "GET" and "/api/subscribers" in path:
            if fail_lookup and fail_lookup():
                raise RuntimeError("listmonk down")
            return {"data": {"results": [{
                "id": 5, "email": "optout@example.com", "name": "Opt Out",
                "lists": [{"id": 1}],
            }], "total": 1}}
        if method == "PUT" and "/api/subscribers/lists" in path:
            modify_calls.append(kwargs.get("json"))
            return {"data": True}
        return {"data": {}}

    from app.services.listmonk_client import ListMonkClient
    lm = ListMonkClient.__new__(ListMonkClient)
    lm._client = None
    lm._request = mock_request
    return lm, modify_calls


@pytest.mark.asyncio
async def test_unsubscribe_scan_processes_requests_the_human_already_read(monkeypatch):
    import app.services.unsubscribe_log as ul
    from app.services import imap_unsubscribe as svc

    # This version has scanned before (the examined set exists); since then
    # the human opened the "remove me" reply in webmail before the next scan.
    await ul.mark_examined_ids([], create=True)
    inbox = _Inbox([
        _mail("prospect@example.com", "Sounds interesting, call me.", "<p1@x>"),
        _mail("optout@example.com", "Please remove me from your list.", "<u1@x>"),
    ], seen=[b"2"])
    monkeypatch.setattr(svc, "connect_imap", lambda: inbox)
    lm, modify_calls = _unsub_client()

    result = await svc.scan_and_unsubscribe(lm)

    assert result["processed"] == 1
    assert len(modify_calls) == 1
    assert inbox.criteria and all(c.startswith("(SINCE ") for c in inbox.criteria)
    # Nothing is downloaded with a \Seen-setting fetch ...
    assert all("PEEK" in spec.upper() for _, spec in inbox.specs)
    # ... and only the handled request is flagged read; the prospect's
    # genuine reply keeps the unread state the human left it in.
    assert inbox.stored == [b"2"]


@pytest.mark.asyncio
async def test_first_scan_seeds_examined_set_from_already_read_mail(monkeypatch):
    """Upgrading must not re-run history: the old scanner flagged everything
    it looked at \\Seen (and "Undo All" relied on that), so mail already read
    at upgrade time is treated exactly as the old version treated it."""
    import app.services.unsubscribe_log as ul
    from app.services import imap_unsubscribe as svc

    inbox = _Inbox([
        _mail("undone@example.com", "remove me", "<old@x>"),
        _mail("optout@example.com", "remove me", "<new@x>"),
    ], seen=[b"1"])
    monkeypatch.setattr(svc, "connect_imap", lambda: inbox)
    lm, modify_calls = _unsub_client()

    result = await svc.scan_and_unsubscribe(lm)

    assert result["processed"] == 1
    assert "<old@x>" in ul.load_examined_ids()
    assert any(c.startswith("(SEEN ") for c in inbox.criteria)

    # The seeding is one-time: a later scan never asks for \Seen again.
    inbox.criteria.clear()
    await svc.scan_and_unsubscribe(lm)
    assert all(c.startswith("(SINCE ") for c in inbox.criteria)


@pytest.mark.asyncio
async def test_unsubscribe_scan_skips_examined_messages_without_download(monkeypatch):
    from app.services import imap_unsubscribe as svc

    inbox = _Inbox([_mail("prospect@example.com", "Thanks, not now.", "<p1@x>")])
    monkeypatch.setattr(svc, "connect_imap", lambda: inbox)
    lm, _ = _unsub_client()

    first = await svc.scan_and_unsubscribe(lm)
    assert first["scanned"] == 1
    inbox.specs.clear()

    second = await svc.scan_and_unsubscribe(lm)
    assert second["scanned"] == 0
    # Only the identity-header peek ran; the body was not fetched again.
    assert all("HEADER.FIELDS" in spec for _, spec in inbox.specs)


@pytest.mark.asyncio
async def test_unsubscribe_scan_retries_a_transient_failure(monkeypatch):
    from app.services import imap_unsubscribe as svc

    inbox = _Inbox([_mail("optout@example.com", "remove me please", "<u1@x>")])
    monkeypatch.setattr(svc, "connect_imap", lambda: inbox)
    down = {"value": True}
    lm, modify_calls = _unsub_client(fail_lookup=lambda: down["value"])

    first = await svc.scan_and_unsubscribe(lm)
    assert first["errors"] == 1 and first["processed"] == 0
    assert inbox.stored == []

    down["value"] = False
    second = await svc.scan_and_unsubscribe(lm)
    assert second["processed"] == 1
    assert len(modify_calls) == 1


def test_fetch_state_ids_batches_and_falls_back():
    from app.services.imap_helpers import fetch_state_ids, message_state_id

    raws = [_mail(f"s{i}@x.com", "hi", f"<m{i}@x>") for i in range(3)]

    class _Partial(_Inbox):
        def fetch(self, num, spec):
            if b"," in num:
                self.specs.append((num, spec))
                # The server answers for the first id only.
                return ("OK", [(b"1 (BODY[HEADER.FIELDS (MESSAGE-ID)] {1}", self.raws[0]), b")"])
            return super().fetch(num, spec)

    conn = _Partial(raws)
    ids = fetch_state_ids(conn, [b"1", b"2", b"3"])
    expected = {
        str(i + 1).encode(): message_state_id(email.message_from_bytes(raws[i]))
        for i in range(3)
    }
    assert ids == expected
    assert sum(1 for num, _ in conn.specs if b"," in num) == 1


def test_unsubscribe_processed_cap_outgrows_link_unsubscribes():
    """The link scanner re-discovers every evicted email (ListMonk keeps them
    "unsubscribed" forever), so the cap must not be a few thousand."""
    import app.services.unsubscribe_log as ul

    assert ul.MAX_PROCESSED_EMAILS >= 50_000


def test_unsubscribe_settings_tolerate_a_non_object_file():
    import app.services.unsubscribe_log as ul

    ul.SETTINGS_FILE.write_text("[1, 2]", encoding="utf-8")
    assert ul.load_settings() == {"blocklist_enabled": False}


# ── Bulk list membership payload is validated ──────────────────────────────

@pytest.mark.parametrize("body", [
    {"ids": "1", "action": "add", "target_list_ids": [1]},
    {"ids": [1], "action": "add", "target_list_ids": []},
    {"ids": [True], "action": "add", "target_list_ids": [1]},
    {"ids": [1], "action": "nuke", "target_list_ids": [1]},
    {"ids": [1], "action": "add", "target_list_ids": [1], "status": "blocklisted"},
])
def test_list_membership_rejects_bad_payloads(client, body):
    with patch("app.routers.subscribers.listmonk.modify_list_memberships",
               new=AsyncMock()) as upstream:
        r = client.put("/api/subscribers/lists", json=body)
    assert r.status_code == 400
    upstream.assert_not_called()


def test_list_membership_forwards_a_rebuilt_payload(client):
    with patch("app.routers.subscribers.listmonk.modify_list_memberships",
               new=AsyncMock(return_value={"data": True})) as upstream:
        r = client.put("/api/subscribers/lists", json={
            "ids": [3, 3, 4], "action": "ADD", "target_list_ids": [7],
            "query": "1=1",
        })
    assert r.status_code == 200
    upstream.assert_awaited_once_with({
        "ids": [3, 4], "action": "add", "target_list_ids": [7], "status": "confirmed",
    })


# ── Replies ────────────────────────────────────────────────────────────────

def test_reply_settings_reject_non_boolean_switches(client):
    r = client.put("/api/replies/settings", json={"auto_blocklist_machine": "false"})
    assert r.status_code == 400
    r = client.put("/api/replies/settings", json={"auto_blocklist_machine": True})
    assert r.status_code == 200
    assert r.json()["auto_blocklist_machine"] is True


def test_reply_search_tolerates_null_fields(client):
    from app.services import reply_log

    reply_log.save_log([{
        "id": "rep_1", "from_email": "a@x.com", "from_name": None,
        "subject": None, "full_body": None, "sentiment": "neutral", "status": "unread",
    }])
    r = client.get("/api/replies?q=a@x")
    assert r.status_code == 200
    assert r.json()["total"] == 1


@pytest.mark.asyncio
async def test_reply_stats_cache_entries_expire_independently(monkeypatch):
    from app.routers import replies as rr

    clock = {"now": 1000.0}
    monkeypatch.setattr(rr.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(rr, "_reply_rate_sent_total", AsyncMock(return_value=0))
    monkeypatch.setattr(rr, "load_log", lambda: [])

    first = await rr.get_reply_stats(campaign_id=0)
    clock["now"] += rr._STATS_CACHE_TTL - 1
    await rr.get_reply_stats(campaign_id=5)          # fresh entry for another key
    clock["now"] += 2                                 # key 0 is now past its TTL
    monkeypatch.setattr(rr, "load_log", lambda: [{"sentiment": "positive"}])
    again = await rr.get_reply_stats(campaign_id=0)
    assert first["total_replies"] == 0
    assert again["total_replies"] == 1


@pytest.mark.parametrize("text,expected", [
    ("I saw this yesterday, will read later", "neutral"),
    ("Our eyes are on Q3 planning", "neutral"),
    ("Love the costume ideas", "neutral"),
    ("Our outlets talk to many vendors", "neutral"),
    ("I am uninterested", "negative"),
    ("Let’s talk next week", "positive"),
    ("We don’t need this right now", "negative"),
    ("Yes please, send it over", "positive"),
    ("What would this cost?", "positive"),
])
def test_sentiment_keywords_are_word_bounded(text, expected):
    from app.services.imap_reply_tracker import classify_sentiment

    assert classify_sentiment("Re: hello", text, "person@company.com") == expected


@pytest.mark.asyncio
async def test_reply_scan_does_not_redownload_processed_messages(monkeypatch):
    """Every 15 minutes the scan re-reads the same 200-message window; only
    unprocessed messages may be downloaded in full."""
    from app.services import imap_reply_tracker as rt
    from app.services import reply_log

    raws = [_mail("lead@x.com", "Let's talk!", "<r1@x>"),
            _mail("other@x.com", "Interested, send pricing", "<r2@x>")]
    inbox = _Inbox(raws)
    monkeypatch.setattr(rt, "_connect_imap", lambda: inbox)
    monkeypatch.setattr(rt.settings, "imap_host", "imap.x")
    monkeypatch.setattr(rt.settings, "imap_user", "me@x.com")
    monkeypatch.setattr(rt.settings, "imap_pass", "pw")
    monkeypatch.setattr(rt, "_fetch_campaigns_covering", AsyncMock(return_value=([], True)))
    monkeypatch.setattr(rt, "match_campaign_for_reply", AsyncMock(return_value=(None, None)))
    await reply_log.mark_processed_message_ids(["<r1@x>"])

    inbox.select = lambda *a, **k: ("OK", [b"1"])
    inbox.close = lambda: ("OK", [b""])
    stats = await rt.scan_and_track_replies(AsyncMock())

    assert stats["new_replies"] == 1
    full_fetches = [num for num, spec in inbox.specs if "HEADER.FIELDS" not in spec]
    assert full_fetches == [b"2"]


# ── Unsubscribe records: deletion scoped to the campaign on screen ─────────

def test_delete_records_scoped_to_campaign(client):
    import app.services.unsubscribe_log as ul

    ul.save_log([
        {"email": "dup@x.com", "campaign_id": 1, "timestamp": "a"},
        {"email": "dup@x.com", "campaign_id": 2, "timestamp": "b"},
    ])
    r = client.delete("/api/unsubscribes/records?emails=DUP@x.com&campaign_id=1")
    assert r.status_code == 200
    assert r.json()["removed"] == 1
    assert [rec["campaign_id"] for rec in ul.load_log()] == [2]


# ── Bounce ingestion: own mailbox and text/rfc822-headers ──────────────────

def test_bounced_recipient_skips_the_bounce_mailbox_itself(monkeypatch):
    from app.services import bounce_ingest as bi

    monkeypatch.setattr(bi.settings, "bounce_imap_user", "bounces@sender.com")
    msg = email.message.EmailMessage()
    msg["X-Original-To"] = "bounces@sender.com"
    msg["From"] = "MAILER-DAEMON@mx.example"
    body = "Final-Recipient: rfc822; dead@prospect.com\nAction: failed\nStatus: 5.1.1\n"
    assert bi._extract_bounced_recipient(msg, body) == "dead@prospect.com"


def test_campaign_hint_read_from_text_rfc822_headers():
    from app.services import bounce_ingest as bi

    raw = (
        b"From: MAILER-DAEMON@mx.example\r\n"
        b"To: bounces@sender.com\r\n"
        b"Subject: Undeliverable\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/report; report-type=delivery-status; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
        b"--B\r\nContent-Type: text/rfc822-headers\r\n\r\n"
        b"From: me@sender.com\r\nTo: dead@prospect.com\r\n"
        b"X-Listmonk-Campaign: 6f1c2d4e-uuid\r\nSubject: Hello\r\n\r\n"
        b"--B--\r\n"
    )
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert bi._extract_campaign_hint(msg) == "6f1c2d4e-uuid"


def test_safe_email_rejects_trailing_newline():
    from app.services.imap_helpers import safe_email_for_query

    assert safe_email_for_query("a@b.com\n") is None
    assert safe_email_for_query("a@b.com") == "a@b.com"
    assert safe_email_for_query(None) is None


# ── CSV converter ──────────────────────────────────────────────────────────

def test_cp1252_csv_keeps_smart_quotes_and_euro():
    from app.services.csv_converter import convert_csv

    raw = "email,company\na@b.co,“Acme” €\n".encode("cp1252")
    result = convert_csv(raw, "email")
    assert result["stats"]["encoding_detected"] == "cp1252"
    row = result["csv_content"].splitlines()[1]
    assert "“Acme” €" in json.loads(row.split(",", 2)[2].strip('"').replace('""', '"'))["company"]


def test_wide_csv_stats_header_stays_bounded(client):
    header = ",".join(["email"] + [f"column_with_a_long_name_{i}" for i in range(300)])
    row = ",".join(["a@b.co"] + ["v"] * 300)
    r = client.post(
        "/api/converter/convert",
        files={"file": ("w.csv", f"{header}\n{row}\n".encode(), "text/csv")},
        data={"email_column": "email"},
    )
    assert r.status_code == 200
    stats_header = r.headers["x-conversion-stats"]
    assert len(stats_header) <= 3000
    stats = json.loads(stats_header)
    assert stats["converted"] == 1 and stats["truncated"] is True


# ── Deliverability: explicit nulls must not fail the audit ─────────────────

@pytest.mark.asyncio
async def test_deliverability_tolerates_null_numeric_settings():
    from app.services.deliverability_service import get_deliverability_checklist

    lm = AsyncMock()
    lm.get_settings = AsyncMock(return_value={"data": {
        "app.batch_size": None, "app.concurrency": None,
    }})
    result = await get_deliverability_checklist(lm)
    statuses = {c["id"]: c["status"] for c in result["checks"]}
    assert statuses["batch_size"] == "fail"
    assert statuses["concurrency"] == "warn"


# ── Bounces router page bound follows the scan bound ───────────────────────

def test_bounce_listing_accepts_deep_pages(client):
    with patch("app.routers.bounces.fetch_filtered_bounces_page",
               new=AsyncMock(return_value={"data": {"results": [], "total": 0}})):
        r = client.get("/api/bounces?page=150&per_page=25&bounce_type=hard")
    assert r.status_code == 200


def test_delete_all_bounces_reports_a_count(client):
    """ListMonk's delete-all returns a bare {"data": true}; the response must
    still carry the shared {deleted, errors, failed} shape."""
    with patch("app.routers.bounces.listmonk.get_bounces",
               new=AsyncMock(return_value={"data": {"results": [], "total": 42}})), \
         patch("app.routers.bounces.listmonk.delete_all_bounces",
               new=AsyncMock(return_value={"data": True})) as wipe, \
         patch("app.routers.bounces.schedule_hard_bounce_update"):
        r = client.delete("/api/bounces?confirm=true")
    assert r.status_code == 200
    assert r.json()["deleted"] == 42 and r.json()["errors"] == 0
    wipe.assert_awaited_once()


# ── Sessions: logout revokes server-side; credential change logs out all ──

class _CookieRequest:
    def __init__(self, token):
        self.cookies = {"lmpro_session": token} if token else {}


def _issue_token():
    from fastapi.responses import JSONResponse
    from app import auth

    resp = JSONResponse({})
    auth.create_session(resp, None)
    cookie = resp.headers["set-cookie"].split(";", 1)[0]
    return cookie.split("=", 1)[1].strip('"')


def test_logout_revokes_the_token_server_side():
    """Logout used to delete only the browser's copy: a copied cookie stayed
    valid for the rest of its 7 days."""
    from starlette.testclient import TestClient
    from app import auth

    token = _issue_token()
    assert auth.verify_session(_CookieRequest(token)) is True
    other = _issue_token()

    c = TestClient(app, raise_server_exceptions=False)
    c.cookies.set(auth.COOKIE_NAME, token)
    r = c.post("/auth/logout", follow_redirects=False)
    assert r.status_code == 303

    assert auth.verify_session(_CookieRequest(token)) is False
    assert auth.verify_session(_CookieRequest(other)) is True, "only that session is revoked"
    # Persisted: a fresh process (empty cache) still refuses it.
    auth._revoked, auth._revoked_mtime = {}, None
    assert auth.verify_session(_CookieRequest(token)) is False


def test_credential_change_invalidates_existing_sessions(monkeypatch):
    from app import auth

    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASS", "old-password")
    token = _issue_token()
    assert auth.verify_session(_CookieRequest(token)) is True

    monkeypatch.setenv("DASHBOARD_PASS", "rotated-password")
    assert auth.verify_session(_CookieRequest(token)) is False


def test_tokens_without_a_revocable_nonce_are_rejected():
    from app import auth

    import time as _time
    ts = str(int(_time.time()))
    legacy = f"{ts}:{auth._sign(ts)}"
    assert auth.verify_session(_CookieRequest(legacy)) is False


def test_expired_revocations_are_pruned():
    from app import auth

    auth._revoked = {"stale": 1.0}
    assert auth.revoke_session(_issue_token()) is True
    stored = json.loads(auth.REVOCATIONS_FILE.read_text())
    assert "stale" not in stored and len(stored) == 1


# ── Scheduler: switching it off releases what it auto-paused ──────────────

@pytest.mark.asyncio
async def test_disabling_the_scheduler_resumes_auto_paused_campaigns(monkeypatch):
    import asyncio
    import app.services.campaign_scheduler as sched

    monkeypatch.setattr(sched, "_SCHED_LOCK", asyncio.Lock())
    sched.save_schedule({**sched.DEFAULT_SCHEDULE, "enabled": False,
                         "auto_paused_campaigns": [1, 2, 3]})

    class _Client:
        def __init__(self):
            self.calls = []

        async def paginate_all(self, fn, per_page=100, status=""):
            # 1 and 2 are still paused; 3 was finished/deleted meanwhile.
            return [{"id": 1, "name": "A", "status": "paused"},
                    {"id": 2, "name": "B", "status": "paused"}] if status == "paused" else []

        async def get_campaigns(self, **kwargs):
            return {}

        async def change_campaign_status(self, cid, status):
            if cid == 2:
                raise RuntimeError("listmonk down")
            self.calls.append((cid, status))

    client = _Client()
    await sched.run_scheduler_tick(client)

    assert client.calls == [(1, "running")]
    # The failed resume stays tracked for the next tick; the vanished one is dropped.
    assert sched.load_schedule()["auto_paused_campaigns"] == [2]


def test_scheduler_put_ticks_when_disabling_too(client):
    with patch("app.main.run_scheduler_tick", AsyncMock()) as tick:
        r = client.put("/api/scheduler", json={"enabled": False})
    assert r.status_code == 200
    tick.assert_awaited_once()


# ── Deliverability: one-click List-Unsubscribe is required ─────────────────

@pytest.mark.asyncio
async def test_list_unsubscribe_header_is_required_and_kept_by_cold_preset():
    from app.services.deliverability_service import (
        apply_deliverability_preset, get_deliverability_checklist,
    )

    lm = AsyncMock()
    lm.get_settings = AsyncMock(return_value={"data": {"privacy.unsubscribe_header": False}})
    checks = {c["id"]: c for c in (await get_deliverability_checklist(lm))["checks"]}
    assert checks["unsubscribe_header"]["status"] == "fail"
    assert checks["unsubscribe_header"]["recommended"] is True

    lm.apply_settings = AsyncMock(return_value={"data": True})
    res = await apply_deliverability_preset(lm, "cold_email")
    assert res["applied"]["privacy.unsubscribe_header"] is True
