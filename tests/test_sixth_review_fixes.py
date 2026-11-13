"""Regressions for the sixth full-codebase review.

Unsubscribe sender from the display name, DSN recipient from X-Original-To,
drafts collecting bounces/unsubscribes, opt-outs dropped without a campaign,
the link scanner cascading non-link unsubscribes, the hourly prune of
deleted-campaign records, the email cleanup's DNS confirmation, manual unblocks
undone by the cleanup, list deletion, default credentials and the smaller
hardening items (id overflow, stuck scan flags, batched header peeks, DMARC
org domain, secret-file mode, pinned CI actions).
"""

import asyncio
import email
import email.policy
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.main import app
from tests.conftest import CSRFTestClient


@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app)


def _msg(text: str) -> email.message.EmailMessage:
    return email.message_from_string(text, policy=email.policy.default)


# ── 1. Unsubscribe sender is the address, never the display name ───────────

@pytest.mark.parametrize("header, expected", [
    ('"victim@corp.com" <attacker@evil.com>', "attacker@evil.com"),
    ('"Smith, John (john.smith@old-corp.com)" <JSmith@Corp.com>', "jsmith@corp.com"),
    ("plain@example.com", "plain@example.com"),
    ("Jane Doe <jane@example.co.uk>", "jane@example.co.uk"),
    ("Undisclosed recipients", None),
])
def test_unsubscribe_sender_comes_from_the_address_not_the_display_name(header, expected):
    from app.services.imap_unsubscribe import _extract_sender_email

    assert _extract_sender_email(_msg(f"From: {header}\nSubject: x\n\nremove me")) == expected


# ── 2. A DSN's X-Original-To is our own address, not the failed recipient ──

def test_dsn_recipient_ignores_x_original_to_alias(monkeypatch):
    from app.config import settings
    from app.services.bounce_ingest import _extract_bounced_recipient

    # The bounce address reaches the mailbox through an alias that is neither
    # IMAP login.
    monkeypatch.setattr(settings, "imap_user", "info@mydomain.com")
    monkeypatch.setattr(settings, "bounce_imap_user", "mailbox@mydomain.com")
    dsn = _msg(
        "From: MAILER-DAEMON@mx.example.net\nX-Original-To: bounces@mydomain.com\n"
        "Subject: Undelivered Mail Returned to Sender\nContent-Type: text/plain\n\n"
        "Final-Recipient: rfc822; dead.user@gmail.com\nAction: failed\nStatus: 5.1.1\n"
    )
    assert _extract_bounced_recipient(dsn, dsn.get_content()) == "dead.user@gmail.com"


# ── 3. A campaign that never sent collects nothing ──────────────────────────

SENT = {"id": 1, "status": "finished", "created_at": "2026-11-01T10:00:00Z",
        "started_at": "2026-11-01T11:00:00Z", "lists": [{"id": 5}], "name": "Sent", "sent": 50}
DRAFT = {"id": 2, "status": "draft", "created_at": "2026-11-10T10:00:00Z",
         "started_at": None, "lists": [{"id": 5}], "name": "Draft"}
SCHEDULED = {"id": 3, "status": "scheduled", "created_at": "2026-11-09T10:00:00Z",
             "started_at": None, "lists": [{"id": 5}], "name": "Scheduled"}
CANCELLED_UNSENT = {"id": 4, "status": "cancelled", "created_at": "2026-11-08T10:00:00Z",
                    "started_at": None, "sent": 0, "lists": [{"id": 5}], "name": "Never started"}
# Drafted before the bounce but only started after it.
LATE_START = {"id": 6, "status": "running", "created_at": "2026-10-20T10:00:00Z",
              "started_at": "2026-11-13T09:00:00Z", "lists": [{"id": 5}], "name": "Late"}
CAMPAIGNS = [DRAFT, SCHEDULED, CANCELLED_UNSENT, SENT, LATE_START]
BOUNCE_AT = datetime(2026, 11, 12, tzinfo=timezone.utc)


def test_bounce_fallback_never_picks_an_unsent_or_later_started_campaign():
    from app.services.bounce_ingest import _pick_campaign

    dsn = _msg("From: MAILER-DAEMON@x\nSubject: failure\n\nbody")
    assert _pick_campaign(dsn, {5}, CAMPAIGNS, BOUNCE_AT)["id"] == 1


def test_link_unsubscribe_attribution_skips_unsent_campaigns():
    from app.services.link_unsubscribe import _pick_campaign_for_list_ids

    assert _pick_campaign_for_list_ids([DRAFT, SCHEDULED, CANCELLED_UNSENT, SENT], {5})["campaign_id"] == 1


def test_reply_and_unsubscribe_matching_skip_unsent_campaigns():
    from app.services.imap_unsubscribe import _match_campaign

    assert _match_campaign(CAMPAIGNS, BOUNCE_AT, {5})["campaign_id"] == 1
    assert _match_campaign([DRAFT], BOUNCE_AT, {5}) is None


def test_campaign_without_status_is_still_attributable():
    from app.services.imap_helpers import campaign_has_sent

    assert campaign_has_sent({"id": 9, "created_at": "2026-01-01"})
    assert campaign_has_sent({"status": "cancelled", "started_at": "2026-01-01T00:00:00Z"})


# ── 4 / 6 / Undo lists: the IMAP unsubscribe scan ──────────────────────────

async def _run_unsub_scan(monkeypatch, subscriber, campaigns, body="Please remove me from your list."):
    from app.services import imap_unsubscribe as svc
    from app.services.listmonk_client import ListMonkClient
    from tests.test_reply_filtering import _UnsubIMAP

    msg = email.message.EmailMessage()
    msg["From"] = f"Someone <{subscriber['email']}>"
    msg["Subject"] = "Re: hello"
    msg["Message-ID"] = "<optout-6@example.com>"
    msg["Date"] = "Wed, 12 Nov 2026 12:00:00 +0000"
    msg.set_content(body)
    monkeypatch.setattr(svc, "connect_imap", lambda: _UnsubIMAP(msg.as_bytes()))

    modify = []

    async def request(method, path, **kwargs):
        if method == "GET" and path == "/api/campaigns":
            return {"data": {"results": campaigns, "total": len(campaigns)}}
        if method == "GET" and "/api/subscribers" in path:
            return {"data": {"results": [subscriber], "total": 1}}
        if method == "PUT" and path == "/api/subscribers/lists":
            modify.append(kwargs.get("json"))
        return {"data": {}}

    lm = ListMonkClient.__new__(ListMonkClient)
    lm._client = None
    lm._request = request
    return await svc.scan_and_unsubscribe(lm), modify


async def test_opt_out_is_honoured_when_no_campaign_matches_and_record_survives(monkeypatch):
    from app.services import unsubscribe_log

    # Mailed on list 2 long ago; that campaign is not in the fetched history.
    subscriber = {"id": 7, "email": "late@example.com", "name": "Late", "status": "enabled",
                  "lists": [{"id": 2, "subscription_status": "confirmed"}]}
    other_list_campaign = {**SENT, "lists": [{"id": 99}]}
    result, modify = await _run_unsub_scan(monkeypatch, subscriber, [other_list_campaign])

    assert result["processed"] == 1
    assert modify == [{"ids": [7], "action": "unsubscribe", "target_list_ids": [2],
                       "status": "unsubscribed"}]
    [record] = unsubscribe_log.load_log()
    assert record["campaign_id"] is None and record["campaign_name"] == "No matching campaign"
    assert record["reattributed_at"]

    # The next scan (complete history, nothing new) neither re-attributes it
    # away nor deletes it.
    await _run_unsub_scan(monkeypatch, {**subscriber, "lists": []}, [other_list_campaign])
    assert [r["email"] for r in unsubscribe_log.load_log()] == ["late@example.com"]


async def test_scan_keeps_records_of_deleted_campaigns(monkeypatch):
    from app.services import unsubscribe_log

    unsubscribe_log.save_log([{
        "email": "old@example.com", "source": "link", "campaign_id": 999,
        "campaign_name": "Deleted", "lists_removed": [1], "subscriber_id": 3,
        "timestamp": "2026-10-01T00:00:00+00:00",
    }])
    subscriber = {"id": 7, "email": "x@example.com", "status": "enabled", "lists": [{"id": 5}]}
    await _run_unsub_scan(monkeypatch, subscriber, [SENT], body="Thanks, sounds good.")
    assert [r["campaign_id"] for r in unsubscribe_log.load_log()] == [999]


async def test_unsubscribe_only_touches_and_records_still_active_lists(monkeypatch):
    from app.services import unsubscribe_log

    subscriber = {"id": 7, "email": "two@example.com", "status": "enabled", "lists": [
        {"id": 1, "subscription_status": "unsubscribed"},   # left earlier, by themselves
        {"id": 5, "subscription_status": "confirmed"},
    ]}
    result, modify = await _run_unsub_scan(monkeypatch, subscriber, [SENT])
    assert result["processed"] == 1
    assert modify[0]["target_list_ids"] == [5]
    assert unsubscribe_log.load_log()[0]["lists_removed"] == [5]   # Undo must not re-add list 1


async def test_unsubscribe_scan_flag_is_released_when_connect_is_cancelled(monkeypatch):
    from app.services import imap_unsubscribe as svc

    def cancelled():
        raise asyncio.CancelledError()

    monkeypatch.setattr(svc, "connect_imap", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await svc.scan_and_unsubscribe(AsyncMock())
    assert svc._scan_running is False


# ── 5. Link scanner: only new unsubscribes, never bounce or app-made ones ───

def _link_client(subs_by_page, bounce_actions=None):
    lm = AsyncMock()
    lm.get_lists.return_value = {"data": {"results": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]}}

    async def by_status(list_id, subscription_status, page=1, per_page=100, query=""):
        lm.queries.append(query)
        rows = [s for s in subs_by_page if any(
            l["id"] == list_id and l.get("subscription_status") == "unsubscribed" for l in s["lists"])]
        return {"data": {"results": rows if page == 1 else []}}

    lm.queries = []
    lm.get_subscribers_by_list_status.side_effect = by_status
    lm.get_campaigns.return_value = {"data": {"results": []}}
    lm.get_settings.return_value = {"data": {"bounce.actions": bounce_actions or {
        "soft": {"count": 2, "action": "none"}, "hard": {"count": 1, "action": "blocklist"},
    }}}
    return lm


def _sub(sid, email, lists):
    return {"id": sid, "email": email, "name": "", "status": "enabled",
            "lists": [{"id": lid, "subscription_status": st} for lid, st in lists]}


async def test_link_scan_does_not_recascade_a_reimported_contact_but_sees_a_new_click():
    from app.services import link_unsubscribe as lu
    from app.services.unsubscribe_log import load_log, mark_processed

    await mark_processed(["back@x.com"])   # handled long ago, then re-imported onto list 2
    reimported = _sub(4, "back@x.com", [(1, "unsubscribed"), (2, "confirmed")])

    lm = _link_client([reimported])
    result = await lu.scan_link_unsubscribes(lm)
    assert result["processed"] == 0
    lm.modify_list_memberships.assert_not_awaited()        # list 2 keeps its new subscription

    # Later they click unsubscribe in a list-2 campaign: that is new.
    clicked = _sub(4, "back@x.com", [(1, "unsubscribed"), (2, "unsubscribed")])
    lm = _link_client([clicked])
    result = await lu.scan_link_unsubscribes(lm)
    assert result["processed"] == 1
    assert load_log()[-1]["lists_removed"] == [2]          # not list 1, left long before


async def test_link_scan_ignores_unsubscribes_made_through_this_app(client):
    from app.routers import subscribers as subs_router
    from app.services import link_unsubscribe as lu

    with patch.object(subs_router.listmonk, "modify_list_memberships",
                      AsyncMock(return_value={"data": True})):
        resp = client.put("/api/subscribers/lists", json={
            "ids": [8], "action": "unsubscribe", "target_list_ids": [1]})
    assert resp.status_code == 200

    lm = _link_client([_sub(8, "moved@x.com", [(1, "unsubscribed"), (2, "confirmed")])])
    result = await lu.scan_link_unsubscribes(lm)
    assert result["processed"] == 0
    lm.modify_list_memberships.assert_not_awaited()        # not cascaded onto list 2


@pytest.mark.parametrize("actions, filtered", [
    ({"hard": {"count": 1, "action": "unsubscribe"}}, True),
    ({"hard": {"count": 1, "action": "blocklist"}}, False),
])
async def test_link_scan_skips_bounced_subscribers_when_bounces_unsubscribe(actions, filtered):
    from app.services import link_unsubscribe as lu

    lm = _link_client([_sub(1, "a@x.com", [(1, "unsubscribed")])], bounce_actions=actions)
    await lu.scan_link_unsubscribes(lm)
    assert all((q == lu.QUERY_NOT_BOUNCED) is filtered for q in lm.queries)


async def test_link_scan_filters_bounced_subscribers_when_settings_are_unreadable():
    from app.services import link_unsubscribe as lu

    lm = _link_client([])
    lm.get_settings.side_effect = RuntimeError("down")
    await lu.scan_link_unsubscribes(lm)
    assert lm.queries and all(q == lu.QUERY_NOT_BOUNCED for q in lm.queries)


def test_undo_all_forgets_the_link_scanner_memory(client):
    from app.routers import unsubscribes as unsub_router
    from app.services import unsubscribe_log

    asyncio.run(unsubscribe_log.add_link_seen({2: [1]}))
    unsubscribe_log.save_log([{"email": "s2@x.com", "subscriber_id": 2, "source": "link",
                               "blocklisted": False, "lists_removed": [1],
                               "timestamp": "2026-11-01T00:00:00+00:00"}])
    lm = unsub_router.listmonk

    async def get_subscriber(sid):
        return {"data": {"id": sid, "email": "s2@x.com", "status": "enabled", "lists": []}}

    with patch.object(lm, "get_subscriber", side_effect=get_subscriber), \
            patch.object(lm, "update_subscriber", AsyncMock(return_value={})), \
            patch.object(lm, "modify_list_memberships", AsyncMock(return_value={})):
        assert client.post("/api/unsubscribes/reset").json()["restored"] == 1
    assert unsubscribe_log.load_link_seen() == {}


def test_undo_all_keeps_the_registry_mark_when_reenabling_fails(client):
    from app.routers import unsubscribes as unsub_router
    from app.services import blocklist_registry, unsubscribe_log

    blocklist_registry.mark([2])
    unsubscribe_log.save_log([{"email": "s2@x.com", "subscriber_id": 2, "source": "email",
                               "blocklisted": True, "lists_removed": [1],
                               "timestamp": "2026-11-01T00:00:00+00:00"}])
    lm = unsub_router.listmonk
    with patch.object(lm, "get_subscriber", AsyncMock(return_value={"data": {
                "id": 2, "email": "s2@x.com", "status": "blocklisted", "lists": []}})), \
            patch.object(lm, "update_subscriber", AsyncMock(side_effect=RuntimeError("down"))):
        assert client.post("/api/unsubscribes/reset").json()["failed"] == 1
    assert blocklist_registry.load() == {2}


# ── 7 / 8. Email cleanup ────────────────────────────────────────────────────

async def test_cleanup_right_after_a_scan_does_not_blocklist_a_dead_domain(monkeypatch):
    from app.services import email_cleanup as ec
    from tests.test_email_validation import FakeListMonk, install_dns, sub

    install_dns(monkeypatch, {"dead.test": "nxdomain"})
    lm = FakeListMonk([sub(1, "gone@dead.test")])
    await ec.run_job(lm, "scan")
    summary = await ec.run_job(lm, "cleanup")      # a minute later: same cached answer
    assert lm.blocklisted == []
    assert summary["pending_confirmation"] == 1
    assert ec.load_results()["gone@dead.test"]["dns_dead_since"]


async def test_cleanup_rechecks_dns_fresh_before_blocklisting(monkeypatch):
    from app.services import email_cleanup as ec
    from tests.test_email_validation import FakeListMonk, fixed_now, install_dns, sub

    install_dns(monkeypatch, {"dead.test": "nxdomain"})
    lm = FakeListMonk([sub(1, "gone@dead.test")])
    await ec.run_job(lm, "scan")
    fixed_now(monkeypatch, datetime.now(timezone.utc) + timedelta(hours=21))

    calls = []
    install_dns(monkeypatch, {"dead.test": "nxdomain"}, calls=calls)
    # The scan inside this cleanup still reads the cached NXDOMAIN; the domain
    # has come back by the time of the fresh re-check.
    original = ec.ev.recheck_domain

    async def revived(domain):
        install_dns(monkeypatch, {})
        return await original(domain)

    monkeypatch.setattr(ec.ev, "recheck_domain", revived)
    summary = await ec.run_job(lm, "cleanup")
    assert lm.blocklisted == []
    assert "fresh check" in summary["message"]


async def test_cleanup_does_not_reblocklist_a_manual_unblock(monkeypatch):
    from app.services import blocklist_registry
    from app.services import email_cleanup as ec
    from tests.test_email_validation import FakeListMonk, install_dns, sub

    install_dns(monkeypatch, {})
    old = {"type": "hard", "email": "back@good.test", "subscriber_id": 1, "campaign": None,
           "created_at": "2026-01-01T00:00:00Z"}
    blocklist_registry.record_unblock([1])
    lm = FakeListMonk([sub(1, "back@good.test")], [old])
    await ec.run_job(lm, "cleanup")
    assert lm.blocklisted == []

    # A bounce AFTER the unblock is new evidence and counts again.
    new = {**old, "created_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()}
    lm = FakeListMonk([sub(1, "back@good.test")], [old, new])
    await ec.run_job(lm, "cleanup")
    assert lm.all_blocklisted() == [1]


def test_unblock_endpoints_record_the_manual_decision(client):
    from app.routers import subscribers as subs_router
    from app.services import blocklist_registry

    current = {"data": {"id": 5, "email": "x@y.com", "status": "blocklisted", "lists": []}}
    with patch.object(subs_router.listmonk, "get_subscriber", AsyncMock(return_value=current)), \
            patch.object(subs_router.listmonk, "update_subscriber", AsyncMock(return_value={})):
        assert client.put("/api/subscribers/5/unblock").status_code == 200
        assert client.put("/api/subscribers/6", json={"status": "enabled"}).status_code == 200
    assert set(blocklist_registry.load_unblocked()) == {5, 6}


def test_registry_reads_the_original_list_format():
    from app.services import blocklist_registry

    blocklist_registry.REGISTRY_FILE.write_text("[3, 4]")
    assert blocklist_registry.load() == {3, 4}
    blocklist_registry.mark_emails(["A@X.com"])
    assert blocklist_registry.load() == {3, 4}
    assert blocklist_registry.load_emails() == {"a@x.com"}
    blocklist_registry.mark([5])
    blocklist_registry.record_unblock([4])
    assert blocklist_registry.load() == {3, 5}
    assert set(blocklist_registry.load_unblocked()) == {4}


# ── Blocklist-mode import is a deliberate blocklisting ──────────────────────

def test_blocklist_import_is_recorded_and_auto_unblock_respects_it(client):
    from app.routers import converter
    from app.services import auto_unblock, blocklist_registry

    csv_bytes = b"email,name\nBlock.Me@x.com,B\nother@x.com,O\n"
    with patch.object(converter.listmonk, "import_subscribers",
                      AsyncMock(return_value={"data": {"status": "importing"}})):
        resp = client.post(
            "/api/converter/convert-and-import",
            files={"file": ("l.csv", csv_bytes, "text/csv")},
            data={"email_column": "email", "name_column": "name",
                  "list_ids": "[1]", "mode": "blocklist"},
        )
    assert resp.status_code == 200, resp.text
    assert blocklist_registry.load_emails() == {"block.me@x.com", "other@x.com"}

    lm = AsyncMock()
    lm.paginate_all.return_value = [{"id": 1, "email": "block.me@x.com"},
                                    {"id": 2, "email": "free@x.com"}]
    kept = asyncio.run(auto_unblock.find_blocklisted_engaged(lm))
    assert [s["id"] for s in kept] == [2]


# ── 9. List deletion keeps shared subscribers and opt-outs ──────────────────

def test_list_delete_keeps_shared_subscribers_and_blocklists_opt_outs(client):
    from app.routers import lists as lists_router
    from app.services import blocklist_registry

    rows = [
        {"id": 1, "email": "only@x.com", "status": "enabled",
         "lists": [{"id": 7, "subscription_status": "confirmed"}]},
        {"id": 2, "email": "shared@x.com", "status": "enabled",
         "lists": [{"id": 7, "subscription_status": "confirmed"},
                   {"id": 8, "subscription_status": "confirmed"}]},
        {"id": 3, "email": "optout@x.com", "status": "enabled",
         "lists": [{"id": 7, "subscription_status": "unsubscribed"}]},
    ]
    lm = AsyncMock()
    lm.get_subscribers.return_value = {"data": {"results": rows, "total": 3}}
    lm.delete_list.return_value = {"data": True}
    with patch.object(lists_router, "listmonk", lm):
        assert client.delete("/api/lists/7").status_code == 200

    lm.delete_subscribers.assert_awaited_once_with([1])
    lm.blocklist_subscribers.assert_awaited_once_with([3])
    assert 3 in blocklist_registry.load()


# ── 10. Default credentials are refused ─────────────────────────────────────

@pytest.mark.parametrize("user, password", [("admin", "changeme"), ("Admin", "admin")])
def test_login_is_refused_while_credentials_are_a_known_default(monkeypatch, user, password):
    from app import auth

    monkeypatch.setenv("DASHBOARD_USER", user)
    monkeypatch.setenv("DASHBOARD_PASS", password)
    assert auth.check_credentials(user, password) is False
    resp = CSRFTestClient(app).post("/auth/login", json={"username": user, "password": password})
    assert resp.status_code == 403
    assert "default" in resp.json()["detail"]
    assert "lmpro_session" not in resp.cookies


# ── Smaller items ───────────────────────────────────────────────────────────

def test_parse_ids_rejects_an_infinite_id(client):
    from app.services.bulk_ops import parse_ids

    with pytest.raises(ValueError):
        parse_ids([float("inf")])
    resp = client.put("/api/subscribers/blocklist", content=b'{"ids": [1e999]}',
                      headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


async def test_bounce_ingest_flag_is_released_when_cancelled(monkeypatch):
    from app.services import bounce_ingest as bi

    class Conn:
        def logout(self):
            return ("BYE", [b""])

    async def cancelled(*a, **k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(bi, "connect_bounce_imap_rw", lambda: Conn())
    monkeypatch.setattr(bi, "_fetch_campaigns_covering", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await bi.ingest_bounce_mailbox(AsyncMock())
    assert bi._ingest_running is False


async def test_bounce_ingest_peeks_headers_in_one_batch(monkeypatch):
    from app.services import bounce_ingest as bi

    class Conn:
        def select(self, *a, **k):
            return ("OK", [b"3"])

        def search(self, *a):
            return ("OK", [b"1 2 3"])

        def logout(self):
            return ("BYE", [b""])

    calls = []

    def batched(conn, ids):
        calls.append(list(ids))
        return {i: "<known@x>" for i in ids}

    monkeypatch.setattr(bi, "connect_bounce_imap_rw", lambda: Conn())
    monkeypatch.setattr(bi, "_fetch_state_ids", batched)
    monkeypatch.setattr(bi, "_fetch_campaigns_covering", AsyncMock(return_value=([], True)))
    bi.mark_processed_ids(["<known@x>"])
    await bi.ingest_bounce_mailbox(AsyncMock())
    assert calls == [[b"1", b"2", b"3"]]


async def test_reply_scan_looks_past_the_newest_200_messages(monkeypatch):
    from app.config import settings
    from app.services import imap_reply_tracker as rt

    monkeypatch.setattr(settings, "imap_host", "imap.test")
    monkeypatch.setattr(settings, "imap_user", "me@test")
    monkeypatch.setattr(settings, "imap_pass", "pw")
    total = 260
    downloads = []

    class Mail:
        def select(self, *a, **k):
            return ("OK", [str(total).encode()])

        def search(self, *a):
            return ("OK", [b" ".join(str(i).encode() for i in range(1, total + 1))])

        def fetch(self, ids, spec):
            if spec == "(RFC822)":
                downloads.append(ids)
                return ("OK", [(ids + b" (RFC822)", f"From: a{int(ids)}@x.test\r\nSubject: hi\r\n"
                                f"Message-ID: <m{int(ids)}@x>\r\n\r\nhello".encode())])
            return ("OK", [(i + b" (BODY[HEADER.FIELDS])", f"Message-ID: <m{int(i)}@x>\r\n\r\n".encode())
                           for i in ids.split(b",")])

        def close(self):
            pass

        def logout(self):
            pass

    monkeypatch.setattr(rt, "_connect_imap", lambda: Mail())
    lm = AsyncMock()
    lm.get_campaigns.return_value = {"data": {"results": []}}
    lm.get_subscribers.return_value = {"data": {"results": []}}
    stats = await rt.scan_and_track_replies(lm)
    assert stats.get("error") is None
    assert len(downloads) == total


def test_reply_scan_window_stays_below_the_processed_id_cap():
    from app.services import imap_reply_tracker as rt, reply_log

    assert 200 < rt.MAX_SCAN_MESSAGES < reply_log.MAX_PROCESSED_IDS


@pytest.mark.parametrize("domain, org", [
    ("mail.acme.co.uk", "acme.co.uk"),
    ("acme.com.au", "acme.com.au"),
    ("news.acme.com", "acme.com"),
    ("acme.de", "acme.de"),
    ("a.b.acme.io", "acme.io"),
])
def test_dmarc_organisational_domain(domain, org):
    from app.services.sending_reputation import _org_domain

    assert _org_domain(domain) == org


def test_session_secret_file_is_never_world_readable(tmp_path):
    from app import auth

    old = os.umask(0)
    try:
        target = tmp_path / "session_secret.key"
        auth._write_private_file(target, "s3cret")
        assert (target.stat().st_mode & 0o777) == 0o600
        target.chmod(0o644)                    # an existing, too-open file is tightened
        auth._write_private_file(target, "s3cret2")
        assert (target.stat().st_mode & 0o777) == 0o600
        assert target.read_text() == "s3cret2"
    finally:
        os.umask(old)


def test_ci_actions_are_pinned_to_commit_shas():
    workflow = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "deploy.yml"
    uses = re.findall(r"^\s*uses:\s*(\S+)", workflow.read_text(), re.MULTILINE)
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref


async def test_fresh_recheck_never_spares_a_hard_bounce_on_a_revived_domain(monkeypatch):
    from app.services import email_cleanup as ec
    from tests.test_email_validation import FakeListMonk, fixed_now, install_dns, sub

    install_dns(monkeypatch, {"flaky.test": "nxdomain"})
    book = [sub(1, "gone@flaky.test"), sub(2, "bounced@flaky.test")]
    bounces = [{"type": "hard", "email": "bounced@flaky.test", "subscriber_id": 2,
                "campaign": None}]
    lm = FakeListMonk(book, bounces)
    await ec.run_job(lm, "scan")
    fixed_now(monkeypatch, datetime.now(timezone.utc) + timedelta(hours=21))
    original = ec.ev.recheck_domain

    async def revived(domain):
        install_dns(monkeypatch, {})
        return await original(domain)

    monkeypatch.setattr(ec.ev, "recheck_domain", revived)
    await ec.run_job(lm, "cleanup")
    assert lm.all_blocklisted() == [2]          # the bounce counts, the domain-only one waits
