"""Regressions for the fifth full-codebase review.

Search passthrough, bounce classification vs IP addresses, the DSN gate on the
bounce mailbox, opt-out keyword boundaries, the link scanner vs blocklisted
subscribers, the deliberate-blocklist registry (auto-unblock), campaign
subject matching, away-notice precision, nested settings secrets, state-file
temp cleanup, the mode-aware audit and the auth cross-site checks.
"""

import email
import email.policy
import os
from unittest.mock import AsyncMock, patch

import pytest

from app.main import app
from tests.conftest import CSRFTestClient


@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app, raise_server_exceptions=False)


def _msg(raw: str) -> email.message.EmailMessage:
    return email.message_from_string(raw, policy=email.policy.default)


# ── Search: ListMonk's list / campaign `query` is plain text, not SQL ───────

def test_plain_search_term_drops_fulltext_operators_only():
    from app.services.search_query import plain_search_term

    assert plain_search_term("  black   friday ") == "black friday"
    assert plain_search_term("o'brien 100%") == "o'brien 100%"      # nothing escaped
    assert plain_search_term("a & b | c:* (x) !y <z>") == "a b c x y z"
    assert plain_search_term("") == "" and plain_search_term(None) == ""


def test_campaign_and_list_search_reach_listmonk_as_typed(client):
    from app.routers import campaigns as camp_router, lists as lists_router

    seen = {}

    async def fake_campaigns(page, per_page, query, status, order_by, order):
        seen["campaigns"] = query
        return {"data": {"results": [], "total": 0}}

    async def fake_lists(page, per_page, query, status, order_by, order, minimal):
        seen["lists"] = query
        return {"data": {"results": [], "total": 0}}

    with patch.object(camp_router.listmonk, "get_campaigns", side_effect=fake_campaigns), \
            patch.object(lists_router.listmonk, "get_lists", side_effect=fake_lists):
        assert client.get("/api/campaigns?search=spring%20(draft)").status_code == 200
        assert client.get("/api/lists?search=leads%20%26%20co").status_code == 200
    assert seen == {"campaigns": "spring draft", "lists": "leads co"}
    assert "ILIKE" not in seen["campaigns"] + seen["lists"]


# ── Bounce classification: status codes vs IP octets ────────────────────────

@pytest.mark.parametrize("body", [
    "451 4.7.1 Greylisted, try again later. Remote host mx.example.net [185.1.1.20]",
    "452 4.2.2 mailbox full (in reply to RCPT TO) relay 95.2.1.7",
    "Deferred by 10.5.1.2; will retry",
    "host 205.4.4.1 said: 421 try again later",
])
def test_ip_address_octets_are_not_hard_status_codes(body):
    from app.services.bounce_ingest import classify_bounce

    assert classify_bounce(body)["type"] == "soft"


@pytest.mark.parametrize("body", [
    "550 5.1.1 The email account that you tried to reach does not exist",
    "Status: 5.1.1",
    "smtp; 550-5.1.1 user unknown",
    "(5.2.1) mailbox disabled",
    "5.4.4 unable to route",
])
def test_real_hard_status_codes_still_classify_hard(body):
    from app.services.bounce_ingest import classify_bounce

    assert classify_bounce(body)["type"] == "hard"


# ── The bounce mailbox only ingests delivery-status notices ─────────────────

def test_looks_like_dsn_accepts_delivery_failures():
    from app.services.bounce_ingest import looks_like_dsn

    report = _msg(
        "From: Mail Delivery Subsystem <mailer-daemon@mx.example>\n"
        "Subject: Undelivered Mail Returned to Sender\n"
        "Content-Type: multipart/report; report-type=delivery-status; boundary=b\n\n"
        "--b\nContent-Type: text/plain\n\nfailed\n--b--\n")
    assert looks_like_dsn(report, report["Subject"], "failed")

    bare = _msg("From: relay@isp.example\nSubject: notice\n\nx")
    assert looks_like_dsn(bare, "notice", "Final-Recipient: rfc822; a@b.com\nAction: failed\nStatus: 5.1.1")
    assert looks_like_dsn(bare, "Delivery Status Notification (Failure)", "")

    exim = _msg("From: someone@isp.example\nX-Failed-Recipients: a@b.com\nSubject: x\n\ny")
    assert looks_like_dsn(exim, "x", "y")


@pytest.mark.parametrize("subject,body", [
    ("Automatic reply: quick question",
     "I am out of the office until Monday. For urgent matters contact my colleague <bob@corp.com>."),
    ("Kawsar - webinar", "Join our webinar <https://x.example/w> this week."),
    ("Re: intro", "Thanks, the status of the project is fine. Action: call me."),
])
def test_looks_like_dsn_rejects_ordinary_mail(subject, body):
    from app.services.bounce_ingest import looks_like_dsn

    msg = _msg(f"From: Jane Doe <jane@corp.com>\nSubject: {subject}\n\n{body}")
    assert not looks_like_dsn(msg, subject, body)


# ── Opt-out keywords are whole words ────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Could you remove mention of pricing from the deck and resend?",
    "We'd exclude media buying, but the rest sounds great - let's talk.",
    "Can your tool remove users in bulk? Interested.",
    "Can we unsubscribe users who bounced automatically?",
])
def test_keyword_inside_another_word_is_not_an_opt_out(text):
    from app.services.imap_unsubscribe import KEYWORD_PATTERN

    assert KEYWORD_PATTERN.search(text) is None


@pytest.mark.parametrize("text", [
    "Remove me.", "please REMOVE US from your list", "exclude me, thanks",
    "Unsubscribe me!", "remove me\nJohn",
])
def test_real_opt_outs_still_match(text):
    from app.services.imap_unsubscribe import KEYWORD_PATTERN

    assert KEYWORD_PATTERN.search(text)


def test_processed_sender_is_only_skipped_while_still_unsubscribed():
    from app.services.imap_unsubscribe import _has_active_subscription

    gone = {"status": "enabled", "lists": [{"id": 1, "subscription_status": "unsubscribed"}]}
    back = {"status": "enabled", "lists": [{"id": 1, "subscription_status": "unsubscribed"},
                                           {"id": 2, "subscription_status": "confirmed"}]}
    blocked = {"status": "blocklisted", "lists": [{"id": 2, "subscription_status": "confirmed"}]}
    assert not _has_active_subscription(gone)
    assert _has_active_subscription(back)          # re-imported: a new request counts
    assert not _has_active_subscription(blocked)
    assert not _has_active_subscription({"status": "enabled", "lists": []})


# ── Link scanner: a blocklisting is not a link unsubscribe ──────────────────

@pytest.mark.asyncio
async def test_link_scan_ignores_blocklisted_subscribers():
    from app.services import link_unsubscribe as lu
    from app.services.unsubscribe_log import load_log

    def sub(sid, email, status):
        return {"id": sid, "email": email, "name": "", "status": status,
                "lists": [{"id": 1, "subscription_status": "unsubscribed"}]}

    lm = AsyncMock()
    lm.get_lists.return_value = {"data": {"results": [{"id": 1, "name": "Leads"}]}}
    lm.get_subscribers_by_list_status.return_value = {"data": {"results": [
        sub(1, "bounced@x.com", "blocklisted"), sub(2, "clicked@x.com", "enabled"),
    ]}}
    lm.get_campaigns.return_value = {"data": {"results": []}}

    result = await lu.scan_link_unsubscribes(lm)

    assert result["processed"] == 1
    records = load_log()
    assert [r["email"] for r in records] == ["clicked@x.com"]
    assert records[0]["blocklisted"] is False


# ── Deliberate blocklistings are never auto-unblocked ───────────────────────

def test_blocklist_registry_round_trip():
    from app.services import blocklist_registry as reg

    assert reg.load() == set()
    reg.mark([5, 7, 5, True, "9", -1, 0])
    assert reg.load() == {5, 7}
    reg.unmark([5])
    assert reg.load() == {7}


@pytest.mark.asyncio
async def test_auto_unblock_skips_deliberately_blocklisted_subscribers():
    from app.services import auto_unblock, blocklist_registry

    blocklist_registry.mark([2])
    lm = AsyncMock()
    lm.paginate_all.return_value = [
        {"id": 1, "email": "false-bounce@x.com"}, {"id": 2, "email": "told-us-no@x.com"},
    ]
    found = await auto_unblock.find_blocklisted_engaged(lm)
    assert [s["id"] for s in found] == [1]


def test_auto_unblock_requires_engagement_after_the_latest_bounce():
    from app.services.auto_unblock import QUERY_BLOCKLISTED_ENGAGED as q

    assert "MAX(created_at) AS last_bounce" in q
    assert "lc.created_at > lb.last_bounce" in q and "cv.created_at > lb.last_bounce" in q


def test_blocklisting_through_the_api_is_registered(client):
    from app.routers import subscribers as subs_router
    from app.services import blocklist_registry

    with patch.object(subs_router.listmonk, "blocklist_subscribers",
                      AsyncMock(return_value={"data": True})), \
            patch.object(subs_router.listmonk, "blocklist_subscriber",
                         AsyncMock(return_value={"data": True})), \
            patch.object(subs_router.listmonk, "update_subscriber",
                         AsyncMock(return_value={"data": {}})), \
            patch.object(subs_router.listmonk, "get_subscriber",
                         AsyncMock(return_value={"data": {"id": 4, "status": "blocklisted"}})):
        assert client.put("/api/subscribers/blocklist", json={"ids": [3, 4]}).status_code == 200
        assert client.put("/api/subscribers/8/blocklist").status_code == 200
        assert blocklist_registry.load() == {3, 4, 8}
        # Re-enabled by hand: no longer a standing decision.
        assert client.put("/api/subscribers/4", json={"status": "enabled"}).status_code == 200
    assert blocklist_registry.load() == {3, 8}


# ── Undo All never lifts a blocklisting the engine did not cause ────────────

def test_reset_leaves_bounce_blocklisted_subscribers_alone(client):
    from app.routers import unsubscribes as unsub_router
    from app.services import unsubscribe_log

    unsubscribe_log.save_log([
        {"email": "dead@x.com", "subscriber_id": 1, "source": "link",
         "lists_removed": [1], "timestamp": "2026-11-01T00:00:00+00:00"},
        {"email": "ours@x.com", "subscriber_id": 2, "source": "link", "blocklisted": True,
         "lists_removed": [1], "timestamp": "2026-11-01T00:00:01+00:00"},
    ])
    lm = unsub_router.listmonk

    async def get_subscriber(sid):
        return {"data": {"id": sid, "email": f"s{sid}@x.com", "status": "blocklisted", "lists": []}}

    with patch.object(lm, "get_subscriber", side_effect=get_subscriber), \
            patch.object(lm, "get_subscriber_bounces",
                         AsyncMock(return_value={"data": [{"id": 9, "type": "hard"}]})), \
            patch.object(lm, "update_subscriber", AsyncMock(return_value={})) as update, \
            patch.object(lm, "modify_list_memberships", AsyncMock(return_value={})):
        resp = client.post("/api/unsubscribes/reset")

    assert resp.status_code == 200
    body = resp.json()
    assert body["restored"] == 1 and body["failed"] == 1
    assert [c.args[0] for c in update.await_args_list] == [2]
    assert [r["email"] for r in unsubscribe_log.load_log()] == ["dead@x.com"]


# ── Reply attribution by subject ────────────────────────────────────────────

def test_short_campaign_subject_only_matches_exactly():
    from app.services.imap_reply_tracker import _clean_subject_for_matching as clean, _subject_matches

    hi = {"id": 1, "subject": "Hi", "name": "c1"}
    assert not _subject_matches(clean("Your shipping update for this week"), hi)
    assert _subject_matches(clean("RE: Re: hi"), hi)
    assert _subject_matches(clean("AW: Hi"), hi)


@pytest.mark.asyncio
async def test_most_specific_subject_wins_over_a_newer_shorter_one():
    from app.services.imap_reply_tracker import match_campaign_for_reply

    campaigns = [
        {"id": 2, "subject": "Quick question", "name": "newer"},
        {"id": 1, "subject": "Quick question about invoices", "name": "older"},
    ]
    cid, _ = await match_campaign_for_reply(
        AsyncMock(), "Re: Quick question about invoices", "a@b.com", campaigns=campaigns,
    )
    assert cid == 1
    cid, _ = await match_campaign_for_reply(
        AsyncMock(), "Re: Quick question", "a@b.com", campaigns=campaigns,
    )
    assert cid == 2


# ── Away wording must be about being away ───────────────────────────────────

@pytest.mark.parametrize("text", [
    "Thanks for reaching out. We mostly focus on holiday rentals, can you send a few numbers?",
    "Our take on vacation packages is different, happy to compare notes.",
    "That decision leaves us with two options.",
])
def test_business_talk_about_holidays_is_not_an_away_notice(text):
    from app.services.imap_reply_tracker import _auto_reply_kind, classify_sentiment

    assert _auto_reply_kind("Re: partnership", text, "ana@villas.com") is None
    assert classify_sentiment("Re: partnership", text, "ana@villas.com") != "auto_reply"


@pytest.mark.parametrize("text", [
    "I am on vacation until Monday 12 May.",
    "I'm currently on holiday and will respond when I return.",
    "On annual leave from 3 to 10 June.",
    "I am on leave.",
    "Currently on holidays, back next week",
])
def test_away_notices_are_still_recognised(text):
    from app.services.imap_reply_tracker import _auto_reply_kind

    assert _auto_reply_kind("Re: partnership", text, "ana@villas.com") == "ooo"


# ── ListMonk settings: masked secrets at any depth ──────────────────────────

def test_masked_secrets_are_blanked_at_every_depth():
    from app.services.listmonk_client import sanitize_settings_for_update

    mask = "•" * 8
    raw = {
        "app.batch_size": 100,
        "smtp": [{"uuid": "u1", "password": mask, "email_headers": [{"X-A": "b"}]}],
        "security.captcha": {"hcaptcha": {"enabled": True, "key": "k", "secret": mask},
                             "altcha": {"enabled": False}},
        "upload.extensions": ["jpg", "png"],
    }
    clean = sanitize_settings_for_update(raw)
    assert clean["smtp"][0] == {"uuid": "u1", "password": "", "email_headers": [{"X-A": "b"}]}
    assert clean["security.captcha"]["hcaptcha"] == {"enabled": True, "key": "k", "secret": ""}
    assert clean["upload.extensions"] == ["jpg", "png"] and clean["app.batch_size"] == 100
    assert raw["security.captcha"]["hcaptcha"]["secret"] == mask        # input untouched


# ── State files: no temp file left behind, no pointless rewrites ────────────

@pytest.mark.parametrize("module_name,file_attr,save", [
    ("warmup", "WARMUP_FILE", lambda m: m.save_state(m.load_state())),
    ("campaign_scheduler", "SCHEDULE_FILE", lambda m: m.save_schedule(m.load_schedule())),
])
def test_failed_state_write_leaves_no_temp_file(monkeypatch, module_name, file_attr, save):
    import importlib

    module = importlib.import_module(f"app.services.{module_name}")
    folder = getattr(module, file_attr).parent

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(module.os, "replace", boom)
    with pytest.raises(OSError):
        save(module)
    assert [p.name for p in folder.iterdir() if p.name.endswith(".tmp")] == []


@pytest.mark.asyncio
async def test_idle_warmup_poll_does_not_rewrite_the_state_file(monkeypatch):
    from app.services import warmup

    await warmup.update_settings({"enabled": True})
    lm = AsyncMock()
    lm.get_running_stats.return_value = {"data": []}
    lm.paginate_all.return_value = []
    await warmup.observe(lm)                      # takes the baseline (a real change)

    writes = []
    real_save = warmup.save_state
    monkeypatch.setattr(warmup, "save_state", lambda s: (writes.append(1), real_save(s)))
    await warmup.observe(lm)
    await warmup.observe(lm)
    assert writes == []


# ── Hard-bounce counts include bounces without a campaign ───────────────────

@pytest.mark.asyncio
async def test_unattributed_hard_bounces_are_counted(monkeypatch):
    from app.services import hard_bounce_cache as hbc
    from app.services.bounce_list import estimate_filtered_hard_total

    class FakeLM:
        _client = object()

        def get_bounces(self, **kw):
            return None

        def get_subscribers(self, **kw):
            return None

        async def iter_pages(self, fn, per_page=500, **kw):
            yield {"id": 1, "type": "hard", "email": "a@x.com", "campaign": {"id": 3}}
            yield {"id": 2, "type": "hard", "email": "b@x.com", "campaign": None}

        async def paginate_all(self, *a, **kw):
            return []

    fake = FakeLM()
    monkeypatch.setattr(hbc, "listmonk", fake)
    await hbc.update_hard_bounce_counts()
    assert hbc.get_all_hard_bounce_counts() == {3: 1, hbc.UNATTRIBUTED: 1}
    assert estimate_filtered_hard_total() == 2
    assert hbc.get_hard_bounce_count(3) == 1


@pytest.mark.asyncio
async def test_background_opener_filter_asks_only_for_bounced_openers():
    from app.services.bounce_filters import filter_bounces_excluding_openers

    lm = AsyncMock()
    lm.paginate_all.return_value = [{"email": "Opened@x.com"}]
    bounces = [
        {"email": "opened@x.com", "campaign": {"id": 7}},
        {"email": "dead@x.com", "campaign": {"id": 7}},
    ]
    kept = await filter_bounces_excluding_openers(lm, bounces)
    assert [b["email"] for b in kept] == ["dead@x.com"]
    query = lm.paginate_all.await_args.kwargs["query"]
    assert "campaign_views WHERE campaign_id=7" in query
    assert "FROM bounces WHERE campaign_id=7" in query
    # Second call inside the TTL costs no query.
    await filter_bounces_excluding_openers(lm, bounces)
    assert lm.paginate_all.await_count == 1


# ── Deliverability audit follows the chosen mode ────────────────────────────

@pytest.mark.asyncio
async def test_newsletter_mode_does_not_fail_cold_only_checks():
    from app.services import app_settings
    from app.services.deliverability_service import (
        COLD_ONLY_CHECKS, apply_deliverability_preset, get_deliverability_checklist,
    )

    lm = AsyncMock()
    lm.get_templates.return_value = {"data": []}
    lm.get_settings.return_value = {"data": {
        "privacy.disable_tracking": False, "privacy.individual_tracking": True,
        "privacy.unsubscribe_header": True, "app.batch_size": 1000, "app.concurrency": 2,
        "app.enable_public_archive": False, "app.enable_public_subscription_page": False,
        "bounce.enabled": True, "bounce.webhooks_enabled": True,
        "bounce.actions": {"hard": {"count": 1, "action": "blocklist"}},
        "upload.extensions": ["jpg"],
    }}

    strict = await get_deliverability_checklist(lm)            # no mode chosen yet
    assert {c["id"] for c in strict["checks"] if c["status"] in ("fail", "warn")} >= {
        "disable_tracking", "batch_size"}

    await app_settings.set_mode("newsletter")
    relaxed = await get_deliverability_checklist(lm)
    assert relaxed["mode"] == "newsletter" and relaxed["score"] == 100
    assert all(c["status"] == "info" for c in relaxed["checks"] if c["id"] in COLD_ONLY_CHECKS)

    await app_settings.set_mode("cold_email")
    assert (await get_deliverability_checklist(lm))["score"] < 100

    applied = (await apply_deliverability_preset(lm, "newsletter"))["applied"]
    assert "app.enable_public_subscription_page" not in applied
    assert "app.enable_public_archive" not in applied


# ── Auth endpoints refuse cross-site posts ──────────────────────────────────

def test_login_requires_a_json_content_type(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASS", "s3cret-pass")
    c = CSRFTestClient(app, raise_server_exceptions=False)
    body = '{"username": "admin", "password": "s3cret-pass"}'

    plain = c.post("/auth/login", content=body, headers={"Content-Type": "text/plain"})
    assert plain.status_code == 415
    cross = c.post("/auth/login", content=body,
                   headers={"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403
    ok = c.post("/auth/login", content=body,
                headers={"Content-Type": "application/json; charset=utf-8",
                         "Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200


def test_cross_site_logout_is_refused():
    c = CSRFTestClient(app, raise_server_exceptions=False)
    refused = c.post("/auth/logout", headers={"Sec-Fetch-Site": "cross-site"},
                     follow_redirects=False)
    assert refused.status_code == 403 and "set-cookie" not in refused.headers
    allowed = c.post("/auth/logout", headers={"Sec-Fetch-Site": "same-origin"},
                     follow_redirects=False)
    assert allowed.status_code == 303
