"""Regression tests for the second full-codebase review (fixes round 2).

Each test names the defect it pins down so a future refactor that reintroduces
the bug fails loudly.
"""

import asyncio
import email as email_mod
import email.policy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.listmonk_client import ListMonkClient


@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield TestClient(app, raise_server_exceptions=False)


# ── 1. RFC 3464 delivery-status fields must be parsed ────────────────────────

# The recipient and enhanced status appear ONLY in the machine-readable
# delivery-status part, which is how many MTAs emit DSNs.
DSN_WITHOUT_TEXT_RECIPIENT = b"""From: MAILER-DAEMON@example.com
To: bounce@example.com
Subject: Undelivered Mail Returned to Sender
Message-ID: <dsn-round2@example.com>
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary="BOUND"

--BOUND
Content-Type: text/plain; charset=utf-8

This is the mail system at host mail.example.com.

I'm sorry to have to inform you that your message could not be delivered.
--BOUND
Content-Type: message/delivery-status

Reporting-MTA: dns; mail.example.com

Final-Recipient: rfc822; no.such.user@example.org
Action: failed
Status: 5.1.1

--BOUND--
"""


def _dsn_message():
    return email_mod.message_from_bytes(
        DSN_WITHOUT_TEXT_RECIPIENT, policy=email_mod.policy.default,
    )


def test_dsn_delivery_status_fields_are_extracted():
    """get_payload(decode=True) is None for container parts; the fields must
    still be readable through the child messages."""
    from app.services.bounce_ingest import _extract_body

    body = _extract_body(_dsn_message())
    assert "Final-Recipient: rfc822; no.such.user@example.org" in body
    assert "Status: 5.1.1" in body


def test_dsn_recipient_only_in_delivery_status_is_found():
    from app.services.bounce_ingest import _extract_body, _extract_bounced_recipient

    msg = _dsn_message()
    assert _extract_bounced_recipient(msg, _extract_body(msg)) == "no.such.user@example.org"


def test_dsn_status_only_in_delivery_status_classifies_hard():
    from app.services.bounce_ingest import _extract_body, classify_bounce

    msg = _dsn_message()
    result = classify_bounce(_extract_body(msg), msg.get("Subject", ""))
    assert result["type"] == "hard"
    assert result["smtp_code"] == "5.1.1"


# ── 2. Policy rejections must not be classified as hard bounces ──────────────

def test_relay_access_denied_is_soft():
    """A relay/policy rejection says nothing about the mailbox being valid."""
    from app.services.bounce_ingest import classify_bounce

    result = classify_bounce("550 5.7.1 Relay access denied", subject="Undelivered")
    assert result["type"] == "soft"
    assert result["reason"] == "soft: relay_denied"


# ── 3. Bounce attribution must cover the whole DSN scan window ───────────────

class _PagedCampaigns:
    def __init__(self, campaigns):
        self.campaigns = campaigns
        self.pages = []

    async def get_campaigns(self, page=1, per_page=100, order_by="", order=""):
        self.pages.append(page)
        chunk = self.campaigns[(page - 1) * per_page: page * per_page]
        return {"data": {"results": chunk, "total": len(self.campaigns)}}


@pytest.mark.asyncio
async def test_bounce_campaign_history_covers_scan_window():
    from app.services.bounce_ingest import _fetch_campaigns_covering

    now = datetime.now(timezone.utc)
    # 4-hour spacing: page 1 only reaches ~17 days back, so the 30-day cutoff
    # forces a second page; page 2 reaches ~33 days and completes the walk.
    camps = [
        {"id": i, "created_at": (now - timedelta(hours=4 * i)).isoformat()}
        for i in range(1, 301)
    ]
    fake = _PagedCampaigns(camps)
    campaigns, complete = await _fetch_campaigns_covering(
        fake, now - timedelta(days=30),
    )
    assert complete is True
    assert len(campaigns) == 200, "history must be walked back until the cutoff is covered"
    assert fake.pages == [1, 2]


@pytest.mark.asyncio
async def test_bounce_campaign_history_reports_truncation():
    from app.services.bounce_ingest import (
        _fetch_campaigns_covering, MAX_CAMPAIGN_PAGES,
    )

    now = datetime.now(timezone.utc)
    camps = [
        {"id": i, "created_at": (now - timedelta(days=i)).isoformat()}
        for i in range(1, 10_000)
    ]
    fake = _PagedCampaigns(camps)
    campaigns, complete = await _fetch_campaigns_covering(
        fake, now - timedelta(days=50_000),
    )
    assert complete is False
    assert len(fake.pages) == MAX_CAMPAIGN_PAGES


# ── 4. ListMonk's bounce type filter is named `type`, not `bounce_type` ──────

@pytest.mark.asyncio
async def test_bounce_type_is_sent_as_type_param():
    captured = {}

    client = ListMonkClient.__new__(ListMonkClient)
    client._client = None

    async def fake_request(method, path, **kwargs):
        captured.update(kwargs.get("params") or {})
        return {"data": {"results": [], "total": 0}}

    client._request = fake_request

    await client.get_bounces(1, 100, bounce_type="hard")
    assert captured.get("type") == "hard"
    assert "bounce_type" not in captured

    captured.clear()
    await client.get_bounces(1, 100, bounce_type="all")
    assert "type" not in captured


def test_invalid_bounce_type_is_rejected(client):
    """ListMonk casts `type` to an enum, so garbage must not be forwarded."""
    resp = client.get("/api/bounces?bounce_type=bogus")
    assert resp.status_code == 400


# ── 5. Bounce exports stream and still 404 on empty results ──────────────────

@pytest.mark.asyncio
async def test_bounce_export_streams_rows(monkeypatch):
    from app.routers import bounces as R

    async def fake_iter(*args, **kwargs):
        yield {
            "id": 1, "email": "a@x.com", "type": "hard", "source": "api",
            "created_at": "2026-01-01T00:00:00Z",
            "campaign": {"id": 7, "name": "September"},
        }

    monkeypatch.setattr(R, "iter_filtered_bounces", fake_iter)

    c = TestClient(app, raise_server_exceptions=False)
    with patch("app.main.verify_session", return_value=True):
        resp = c.get("/api/bounces/export?bounce_type=hard")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "a@x.com" in resp.text and "September" in resp.text


@pytest.mark.asyncio
async def test_bounce_export_404_when_empty(monkeypatch):
    from app.routers import bounces as R

    async def empty_iter(*args, **kwargs):
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(R, "iter_filtered_bounces", empty_iter)

    c = TestClient(app, raise_server_exceptions=False)
    with patch("app.main.verify_session", return_value=True):
        resp = c.get("/api/bounces/export?bounce_type=hard")

    assert resp.status_code == 404


def test_delete_all_bounces_validates_type(client):
    assert client.delete("/api/bounces?bounce_type=nope").status_code == 400


# ── 6. Campaign subscriber exports stream and keep transformed columns ───────

@pytest.mark.asyncio
async def test_campaign_subscriber_export_streams(monkeypatch):
    from app.routers import campaigns as C

    async def fake_iter_pages(fetch_fn, per_page=100, max_pages=0, **kwargs):
        yield {
            "id": 1, "email": "a@x.com", "name": "A", "status": "enabled",
            "lists": [{"id": 1, "name": "Newsletter"}],
            "attribs": {"city": "Dhaka"},
            "created_at": "2026-01-01",
        }

    monkeypatch.setattr(C.listmonk, "iter_pages", fake_iter_pages)

    c = TestClient(app, raise_server_exceptions=False)
    with patch("app.main.verify_session", return_value=True):
        resp = c.get("/api/campaigns/1/subscribers/views/export")

    assert resp.status_code == 200
    assert "a@x.com" in resp.text
    assert "Newsletter" in resp.text        # lists flattened to names
    assert "Dhaka" in resp.text             # attribs serialized to JSON


@pytest.mark.asyncio
async def test_campaign_bounce_export_streams(monkeypatch):
    from app.routers import campaigns as C

    async def fake_iter(*args, **kwargs):
        yield {
            "id": 3, "email": "bounced@x.com", "type": "hard", "source": "api",
            "created_at": "2026-01-01T00:00:00Z", "campaign": {"id": 1, "name": "C"},
        }

    monkeypatch.setattr(C, "iter_filtered_bounces", fake_iter)

    c = TestClient(app, raise_server_exceptions=False)
    with patch("app.main.verify_session", return_value=True):
        resp = c.get("/api/campaigns/1/subscribers/bounces/export")

    assert resp.status_code == 200
    assert "bounced@x.com" in resp.text


# ── 7. Link scanner must not silently stop at the first page of lists ────────

@pytest.mark.asyncio
async def test_link_list_fetch_pages_all_lists():
    from app.services.link_unsubscribe import _fetch_all_lists

    class FakeLM:
        def __init__(self):
            self.pages = []

        async def get_lists(self, page=1, per_page=100, **kwargs):
            self.pages.append(page)
            start = 1 + (page - 1) * 100
            chunk = [{"id": i, "name": "L"} for i in range(start, start + 100)]
            if page > 2:
                chunk = []
            return {"data": {"results": chunk, "total": 250}}

    fake = FakeLM()
    lists = await _fetch_all_lists(fake)
    assert len(lists) == 200
    assert fake.pages == [1, 2, 3]


@pytest.mark.asyncio
async def test_link_list_fetch_tolerates_minimal_single_page():
    """With minimal=true, ListMonk may return every list on page 1; the fetch
    must not append duplicates when page 2 repeats the same rows."""
    from app.services.link_unsubscribe import _fetch_all_lists

    class FakeLM:
        calls = 0

        async def get_lists(self, page=1, per_page=100, **kwargs):
            FakeLM.calls += 1
            return {
                "data": {"results": [{"id": i} for i in range(1, 251)], "total": 250},
            }

    lists = await _fetch_all_lists(FakeLM())
    assert len(lists) == 250
    assert len({lst["id"] for lst in lists}) == 250
    assert FakeLM.calls == 2


# ── 8. Upstream ListMonk auth failures must not look like session expiry ─────

def test_upstream_auth_error_is_mapped_to_502(client):
    request = httpx.Request("GET", "https://listmonk.example/api/campaigns")
    response = httpx.Response(401, json={"message": "unauthorized"}, request=request)
    exc = httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)

    with patch(
        "app.routers.campaigns.listmonk.get_campaigns",
        new=AsyncMock(side_effect=exc),
    ):
        resp = client.get("/api/campaigns")

    assert resp.status_code == 502, "a 401 here makes the SPA redirect to a login loop"
    assert "LISTMONK_API_KEY" in resp.json()["detail"]


def test_upstream_validation_error_keeps_its_status(client):
    request = httpx.Request("POST", "https://listmonk.example/api/campaigns")
    response = httpx.Response(
        400, json={"message": "invalid campaign"}, request=request,
    )
    exc = httpx.HTTPStatusError("400 Bad Request", request=request, response=response)

    with patch(
        "app.routers.campaigns.listmonk.create_campaign",
        new=AsyncMock(side_effect=exc),
    ):
        resp = client.post("/api/campaigns", json={})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid campaign"


def test_unreachable_listmonk_is_502(client):
    request = httpx.Request("GET", "https://listmonk.example/api/campaigns")
    exc = httpx.ConnectError("connection refused", request=request)

    with patch(
        "app.routers.campaigns.listmonk.get_campaigns",
        new=AsyncMock(side_effect=exc),
    ):
        resp = client.get("/api/campaigns")

    assert resp.status_code == 502
    assert "Cannot reach the ListMonk API" in resp.json()["detail"]


# ── 9. Reply scans are serialised ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reply_scan_is_serialised():
    from app.services import imap_reply_tracker as rt

    async with rt._reply_scan_lock:
        result = await rt.scan_and_track_replies(object())

    assert result["message"] == "Reply scan already in progress"
    assert result["new_replies"] == 0


# ── 10. Reply attribution: no blind "most recent campaign" fallback ──────────

def test_short_reply_subject_cannot_match_by_substring():
    from app.services.imap_reply_tracker import _subject_matches

    assert not _subject_matches("hi", {"subject": "", "name": "High-volume outreach"})
    assert not _subject_matches("re: hello", {"subject": "", "name": ""})
    assert _subject_matches(
        "re: quick question", {"subject": "Quick question", "name": "X"},
    )


@pytest.mark.asyncio
async def test_reply_is_never_attributed_by_recency_alone():
    from app.services import imap_reply_tracker as rt

    now = datetime.now(timezone.utc)
    campaigns = [{
        "id": 9, "name": "Unrelated", "subject": "Big sale",
        "created_at": now.isoformat(), "lists": [{"id": 1}],
    }]
    lm = AsyncMock()
    lm.get_subscribers = AsyncMock(return_value={"data": {"results": []}})

    cid, name = await rt.match_campaign_for_reply(
        lm, "Re: something else entirely", "stranger@x.com", now, campaigns=campaigns,
    )
    assert (cid, name) == (None, None)


@pytest.mark.asyncio
async def test_reply_falls_back_to_list_aware_matching():
    from app.services import imap_reply_tracker as rt

    now = datetime.now(timezone.utc)
    campaigns = [
        {"id": 5, "name": "September outreach", "subject": "Quick question",
         "created_at": (now - timedelta(days=20)).isoformat(), "lists": [{"id": 3}]},
        {"id": 7, "name": "Newer campaign", "subject": "Other",
         "created_at": (now - timedelta(days=2)).isoformat(), "lists": [{"id": 4}]},
    ]
    lm = AsyncMock()
    lm.get_subscribers = AsyncMock(return_value={
        "data": {"results": [{"id": 1, "lists": [{"id": 3}]}]},
    })

    cid, name = await rt.match_campaign_for_reply(
        lm, "Re: totally different topic", "lead@x.com", now, campaigns=campaigns,
    )
    assert cid == 5, "must attribute to the campaign that targeted the sender's list"
    assert name == "September outreach"


# ── 11. Shared quote stripper handles multi-line "On ... wrote:" headers ─────

def test_multiline_on_wrote_header_is_stripped():
    from app.services.imap_unsubscribe import _extract_reply_only

    body = (
        "Sure, remove me.\n\n"
        "On Mon, Jan 1, 2026 at 12:00 PM\n"
        "John Doe <john@example.com> wrote:\n"
        "> original text\n> Remove me\n"
    )
    assert _extract_reply_only(body) == "Sure, remove me."


def test_reply_tracker_uses_shared_stripper():
    from app.services.imap_reply_tracker import extract_clean_reply

    body = (
        "Interested, call me.\n\n"
        "On Mon, Jan 1, 2026 at 12:00 PM\n"
        "John Doe <john@example.com> wrote:\n"
        "> Our newsletter\n> Remove me\n"
    )
    assert extract_clean_reply(body) == "Interested, call me."


# ── 11b. Reply display text is tidied (image markers, blank runs) ────────────

def test_clean_reply_drops_image_placeholders_and_blank_runs():
    from app.services.imap_reply_tracker import extract_clean_reply

    body = (
        "Who's this?\n\n\n\n"
        "Kind Regards,\n\n"
        "[image: image]\n\n"
        "Tevyn Chubinski   \n"
    )
    cleaned = extract_clean_reply(body)
    assert "[image" not in cleaned
    assert "\n\n\n" not in cleaned
    assert cleaned.endswith("Tevyn Chubinski")
    assert "Who's this?" in cleaned


def test_clean_reply_strips_wrapped_on_wrote_tail():
    from app.services.imap_reply_tracker import extract_clean_reply

    # Shape seen in production: narrow no-break space in the date and the
    # "wrote:" marker pushed onto the next line.
    body = (
        "Who's this?\n\n"
        "Email Pixelpulsemediamb@gmail.com\n\n"
        "On Tue, Oct 6, 2026 at 7:49\u202fPM Kawsar Mahmud <kawsar@getaevoramedia.com>\n"
        "wrote:"
    )
    cleaned = extract_clean_reply(body)
    assert "wrote:" not in cleaned
    assert cleaned.endswith("Email Pixelpulsemediamb@gmail.com")


# ── 12. Blocklisting a reply resolves the email server-side ──────────────────

def _reply_log(tmp_path, monkeypatch, records):
    import app.services.reply_log as rl

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "reply_log.json")
    monkeypatch.setattr(rl, "PROCESSED_FILE", tmp_path / "reply_processed.json")
    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    rl.save_log(records)


def test_blocklist_reply_endpoint(client, tmp_path, monkeypatch):
    from app.routers import replies as R

    _reply_log(tmp_path, monkeypatch, [
        {"id": "rep_1", "from_email": "lead@example.com", "from_name": "Lead"},
    ])

    with patch.object(
        R.listmonk, "get_subscribers",
        new=AsyncMock(return_value={"data": {"results": [{"id": 42}]}}),
    ), patch.object(
        R.listmonk, "blocklist_subscriber", new=AsyncMock(return_value={"data": True}),
    ) as blocklist:
        resp = client.post("/api/replies/rep_1/blocklist")

    assert resp.status_code == 200
    assert resp.json()["subscriber_id"] == 42
    blocklist.assert_awaited_once_with(42)


def test_blocklist_reply_rejects_injected_email(client, tmp_path, monkeypatch):
    """A crafted Reply-To must not reach the ListMonk query string."""
    _reply_log(tmp_path, monkeypatch, [
        {"id": "rep_2", "from_email": "x' OR 1=1 --@evil.com"},
    ])
    resp = client.post("/api/replies/rep_2/blocklist")
    assert resp.status_code == 400


def test_blocklist_reply_unknown_id_is_404(client, tmp_path, monkeypatch):
    _reply_log(tmp_path, monkeypatch, [])
    assert client.post("/api/replies/nope/blocklist").status_code == 404


# ── 13. Reply rate uses every campaign's sent total ─────────────────────────

def test_reply_stats_use_full_sent_total(client, tmp_path, monkeypatch):
    _reply_log(tmp_path, monkeypatch, [
        {"id": f"rep_{i}", "sentiment": "positive"} for i in range(5)
    ])

    with patch(
        "app.routers.campaigns.get_campaigns_summary",
        new=AsyncMock(return_value={"sent": 1000}),
    ):
        resp = client.get("/api/replies/stats")

    data = resp.json()
    assert data["total_sent"] == 1000
    assert data["total_replies"] == 5
    assert data["reply_rate"] == 0.5


# ── 13b. Reply stats can be scoped to a single campaign ─────────────────────

def test_reply_stats_scoped_to_campaign(client, tmp_path, monkeypatch):
    """The Analytics page asks for one campaign's reply rate, so the endpoint
    must filter the counts and use that campaign's sent total as denominator
    while still returning the full per-campaign breakdown for the chart."""
    _reply_log(tmp_path, monkeypatch, [
        {"id": "rep_1", "sentiment": "positive", "campaign_id": 7},
        # Tracker records have used both ints and strings for campaign_id.
        {"id": "rep_2", "sentiment": "auto_reply", "campaign_id": "7"},
        {"id": "rep_3", "sentiment": "positive", "campaign_id": 9},
    ])

    with patch(
        "app.routers.replies.listmonk.get_campaign",
        new=AsyncMock(return_value={"data": {"id": 7, "sent": 200}}),
    ) as get_campaign:
        resp = client.get("/api/replies/stats?campaign_id=7")

    data = resp.json()
    assert data["campaign_id"] == 7
    assert data["total_replies"] == 2
    assert data["positive"] == 1
    assert data["auto_reply"] == 1
    assert data["negative"] == 0
    assert data["total_sent"] == 200
    # Auto-replies are excluded from the reply rate: only the 1 human reply
    # counts, not the automated response.
    assert data["human_replies"] == 1
    assert data["reply_rate"] == 0.5
    # The breakdown always covers every campaign, not just the scoped one.
    assert data["by_campaign"] == {"7": 2, "9": 1}
    get_campaign.assert_awaited_once_with(7)


def test_reply_stats_survive_campaign_lookup_failure(client, tmp_path, monkeypatch):
    """A ListMonk hiccup must not 500 the stats endpoint; the rate degrades to 0."""
    _reply_log(tmp_path, monkeypatch, [
        {"id": "rep_1", "sentiment": "positive", "campaign_id": 7},
    ])

    with patch(
        "app.routers.replies.listmonk.get_campaign",
        new=AsyncMock(side_effect=RuntimeError("listmonk down")),
    ):
        resp = client.get("/api/replies/stats?campaign_id=7")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_replies"] == 1
    assert data["total_sent"] == 0
    assert data["reply_rate"] == 0.0


def test_reply_stats_hide_leftover_bounce_records(client, tmp_path, monkeypatch):
    """DSNs are ingested as bounces, never stored as replies. The read filter
    is a defensive guard: a bounce-classified record left in a log written by
    an older version can never surface in the totals or the campaign chart."""
    _reply_log(tmp_path, monkeypatch, [
        {"id": "rep_1", "sentiment": "positive", "campaign_id": 7},
        {"id": "rep_2", "sentiment": "bounce", "campaign_id": 7},
        {"id": "rep_3", "sentiment": "negative", "campaign_id": 9},
    ])

    with patch(
        "app.routers.replies.listmonk.get_campaign",
        new=AsyncMock(return_value={"data": {"id": 7, "sent": 200}}),
    ):
        resp = client.get("/api/replies/stats?campaign_id=7")

    data = resp.json()
    assert data["total_replies"] == 1          # the DSN is not a reply
    assert data["positive"] == 1
    assert data["negative"] == 0               # scoped to campaign 7
    assert data["total_sent"] == 200
    assert data["reply_rate"] == 0.5           # 1/200, not 2/200
    assert data["by_campaign"] == {"7": 1, "9": 1}  # bounce not counted


def test_reply_stats_exclude_auto_replies_from_rate(client, tmp_path, monkeypatch):
    """Automated responses (OOO, ticket confirmations) must not inflate the
    reply rate — only genuine human replies count toward the metric."""
    _reply_log(tmp_path, monkeypatch, [
        {"id": "rep_1", "sentiment": "positive"},
        {"id": "rep_2", "sentiment": "auto_reply"},
        {"id": "rep_3", "sentiment": "auto_reply"},
        {"id": "rep_4", "sentiment": "neutral"},
        {"id": "rep_5", "sentiment": "negative"},
    ])

    with patch(
        "app.routers.campaigns.get_campaigns_summary",
        new=AsyncMock(return_value={"sent": 1000}),
    ):
        resp = client.get("/api/replies/stats")

    data = resp.json()
    assert data["total_replies"] == 5
    assert data["auto_reply"] == 2
    assert data["human_replies"] == 3          # 5 - 2 automated
    assert data["reply_rate"] == 0.3           # 3/1000, not 5/1000


# ── 14. Auto-unblock runs concurrently and preserves list memberships ────────

@pytest.mark.asyncio
async def test_unblock_subscribers_is_concurrent_and_keeps_lists():
    from app.services.auto_unblock import unblock_subscribers, UNBLOCK_CONCURRENCY

    inflight = 0
    peak = 0
    payloads = []

    class FakeLM:
        async def update_subscriber(self, sid, data):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0)
            payloads.append(data)
            inflight -= 1

        async def get_subscriber_bounces(self, sid):
            return {"data": []}

    subs = [
        {"id": i, "email": f"u{i}@x.com", "name": "", "lists": [{"id": 1}],
         "attribs": {}}
        for i in range(1, 41)
    ]
    result = await unblock_subscribers(FakeLM(), subs)

    assert result["success"] == 40
    assert result["failed"] == 0
    assert peak > 1, "subscribers were still processed serially"
    assert peak <= UNBLOCK_CONCURRENCY
    assert all(p["lists"] == [1] for p in payloads)


# ── 15. Unsubscribe scan looks back at least MIN_LOOKBACK_DAYS ───────────────

class _RecordingIMAP:
    def __init__(self):
        self.criteria = None

    def select(self, mailbox):
        return ("OK", [b"1"])

    def search(self, charset, criteria):
        self.criteria = criteria
        return ("OK", [b""])

    def logout(self):
        return ("BYE", [b""])


@pytest.mark.asyncio
async def test_imap_scan_looks_back_at_least_min_days(tmp_path, monkeypatch):
    import app.services.unsubscribe_log as shared_log
    from app.services import imap_unsubscribe as svc
    from app.services.imap_helpers import imap_date

    monkeypatch.setattr(shared_log, "LOG_FILE", tmp_path / "log.json")
    monkeypatch.setattr(shared_log, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(shared_log, "PROCESSED_FILE", tmp_path / "processed.json")
    shared_log.save_log([])

    fake = _RecordingIMAP()
    monkeypatch.setattr(svc, "connect_imap", lambda: fake)

    # Newest campaign is *recent*: the campaign-month anchor alone would only
    # cover a few days and miss replies to last month's campaign.
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

    async def mock_request(method, path, **kwargs):
        if "/api/campaigns" in path:
            return {"data": {"results": [
                {"id": 1, "name": "New", "created_at": recent, "lists": [{"id": 1}]},
            ], "total": 1}}
        return {"data": {}}

    client = ListMonkClient.__new__(ListMonkClient)
    client._client = None
    client._request = mock_request

    await svc.scan_and_unsubscribe(client)

    expected = imap_date(datetime.now(timezone.utc) - timedelta(days=svc.MIN_LOOKBACK_DAYS))
    assert fake.criteria == f"(UNSEEN SINCE {expected})"


# ── 16. Processed message IDs are persisted in one batch write ───────────────

def test_processed_message_ids_are_batched(tmp_path, monkeypatch):
    import app.services.reply_log as rl

    monkeypatch.setattr(rl, "PROCESSED_FILE", tmp_path / "processed.json")
    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "log.json")

    rl.mark_processed_message_ids(["<a>", "<b>"])
    assert json.loads((tmp_path / "processed.json").read_text()) == ["<a>", "<b>"]

    rl.mark_processed_message_ids(["<b>", "<c>"])
    assert json.loads((tmp_path / "processed.json").read_text()) == ["<a>", "<b>", "<c>"]


# ── 17. fetch_all_filtered_bounces still drives the streaming iterator ───────

@pytest.mark.asyncio
async def test_fetch_all_filtered_bounces_still_filters_and_pages():
    from app.services.bounce_list import fetch_all_filtered_bounces

    def make_page(start: int) -> dict:
        # One hard bounce plus filler soft bounces, so each 100-row page
        # yields exactly one hard bounce after server-side filtering.
        results = [
            {"id": start, "email": f"u{start}@x.com", "type": "hard",
             "campaign": {"id": 7}},
        ]
        results += [
            {"id": start + i, "email": f"u{start + i}@x.com", "type": "soft",
             "campaign": {"id": 7}}
            for i in range(1, 100)
        ]
        return {"data": {"results": results, "total": 200}}

    calls = {"n": 0}

    async def fake_get_bounces(page=1, per_page=100, campaign_id=None, source="", bounce_type=""):
        calls["n"] += 1
        return make_page(1 if page == 1 else 101)

    lm = AsyncMock()
    lm.get_bounces = AsyncMock(side_effect=fake_get_bounces)
    # No openers -> nothing is excluded
    lm.get_subscribers = AsyncMock(return_value={"data": {"total": 0, "results": []}})

    bounces = await fetch_all_filtered_bounces(lm, "hard")
    assert [b["id"] for b in bounces] == [1, 101]
    assert calls["n"] == 2, "the iterator must stop once `total` is satisfied"