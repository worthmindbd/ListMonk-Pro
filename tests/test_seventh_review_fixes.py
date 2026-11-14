"""Regression tests for the seventh full review.

The link unsubscribe scanner's memory (which lists it already handled) had
to agree with everything else that changes subscriptions: unblocking
(manual, Edit form, auto-unblock), Undo All, re-subscriptions and the IMAP
opt-out scanner. Plus: an IMAP blocklist failure, the Edit form's status
bookkeeping and overlapping auto-unblock runs.
"""

import asyncio
import email
import email.policy
import inspect
from unittest.mock import AsyncMock, patch

import pytest

from app.main import app
from tests.conftest import CSRFTestClient
from tests.test_sixth_review_fixes import SENT, _link_client, _sub


@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app)


def _blocklisted(sid, email_addr, list_ids):
    """What ListMonk returns for a blocklisted subscriber: every subscription
    was marked unsubscribed by the blocklisting."""
    return {"id": sid, "email": email_addr, "name": "", "status": "blocklisted", "attribs": {},
            "lists": [{"id": lid, "subscription_status": "unsubscribed"} for lid in list_ids]}


def _after_unblock(sid, email_addr, list_ids):
    """Re-enabled, subscriptions still unsubscribed (ListMonk keeps them)."""
    return _sub(sid, email_addr, [(lid, "unsubscribed") for lid in list_ids])


# ── 1. Unblocking is not mistaken for a link unsubscribe ───────────────────

async def test_auto_unblocked_contact_is_not_logged_as_a_link_unsubscribe():
    from app.services import auto_unblock, link_unsubscribe as lu
    from app.services.unsubscribe_log import load_log, save_settings

    save_settings({"blocklist_enabled": True})
    lm = AsyncMock()
    lm.get_subscriber_bounces.return_value = {"data": []}
    result = await auto_unblock.unblock_subscribers(lm, [_blocklisted(9, "back@x.com", [1, 2])])
    assert result["success"] == 1

    scan_lm = _link_client([_after_unblock(9, "back@x.com", [1, 2])])
    res = await lu.scan_link_unsubscribes(scan_lm)
    assert res["processed"] == 0 and load_log() == []
    scan_lm.blocklist_subscriber.assert_not_awaited()       # not blocklisted again


def test_manual_unblock_is_not_logged_as_a_link_unsubscribe(client):
    from app.routers import subscribers as subs_router
    from app.services import link_unsubscribe as lu

    with patch.object(subs_router.listmonk, "get_subscriber",
                      AsyncMock(return_value={"data": _blocklisted(5, "m@x.com", [1])})), \
            patch.object(subs_router.listmonk, "update_subscriber", AsyncMock(return_value={})):
        assert client.put("/api/subscribers/5/unblock").status_code == 200

    res = asyncio.run(lu.scan_link_unsubscribes(_link_client([_after_unblock(5, "m@x.com", [1])])))
    assert res["processed"] == 0


def test_edit_form_unblock_is_not_logged_as_a_link_unsubscribe(client):
    from app.routers import subscribers as subs_router
    from app.services import blocklist_registry, link_unsubscribe as lu

    with patch.object(subs_router.listmonk, "get_subscriber",
                      AsyncMock(return_value={"data": _blocklisted(6, "e@x.com", [2])})), \
            patch.object(subs_router.listmonk, "update_subscriber", AsyncMock(return_value={})):
        assert client.put("/api/subscribers/6", json={
            "email": "e@x.com", "name": "", "status": "enabled", "attribs": {}, "lists": [2],
        }).status_code == 200
    assert 6 in blocklist_registry.load_unblocked()

    res = asyncio.run(lu.scan_link_unsubscribes(_link_client([_after_unblock(6, "e@x.com", [2])])))
    assert res["processed"] == 0


# ── 2. Undo All is not undone by the next link scan ─────────────────────────

def test_undo_keeps_lists_left_earlier_handled_so_the_restore_sticks(client):
    from app.routers import unsubscribes as unsub_router
    from app.services import link_unsubscribe as lu, unsubscribe_log

    # IMAP opt-out removed list 2; list 1 had been left long before.
    unsubscribe_log.save_log([{"email": "u@x.com", "subscriber_id": 5, "source": "email",
                               "blocklisted": False, "lists_removed": [2],
                               "timestamp": "2026-11-01T00:00:00+00:00"}])
    asyncio.run(unsubscribe_log.mark_processed(["u@x.com"]))
    before = _sub(5, "u@x.com", [(1, "unsubscribed"), (2, "unsubscribed")])
    lm = unsub_router.listmonk
    with patch.object(lm, "get_subscriber", AsyncMock(return_value={"data": before})), \
            patch.object(lm, "update_subscriber", AsyncMock(return_value={})), \
            patch.object(lm, "modify_list_memberships", AsyncMock(return_value={})):
        assert client.post("/api/unsubscribes/reset").json()["restored"] == 1
    assert unsubscribe_log.load_link_seen() == {5: {1}}

    restored = _sub(5, "u@x.com", [(1, "unsubscribed"), (2, "confirmed")])
    scan_lm = _link_client([restored])
    assert asyncio.run(lu.scan_link_unsubscribes(scan_lm))["processed"] == 0
    scan_lm.modify_list_memberships.assert_not_awaited()     # list 2 stays restored

    # A real click on the restored list later is still a new unsubscribe.
    clicked = _sub(5, "u@x.com", [(1, "unsubscribed"), (2, "unsubscribed")])
    assert asyncio.run(lu.scan_link_unsubscribes(_link_client([clicked])))["processed"] == 1


# ── 3. A re-subscribed contact's next click counts ──────────────────────────

async def test_fully_resubscribed_contact_clicking_again_is_processed():
    from app.services import link_unsubscribe as lu

    first = _sub(6, "r@x.com", [(1, "unsubscribed")])
    assert (await lu.scan_link_unsubscribes(_link_client([first])))["processed"] == 1

    # Re-imported onto list 1 (no unsubscribed list left): the scan does not
    # meet them in its unsubscribed pages, so it looks them up by id.
    lm = _link_client([])
    lm.get_subscribers.return_value = {"data": {"results": [_sub(6, "r@x.com", [(1, "confirmed")])]}}
    await lu.scan_link_unsubscribes(lm)
    query = lm.get_subscribers.await_args.args[2]
    assert "subscribers.id IN (6)" in query and "status != 'blocklisted'" in query

    again = _sub(6, "r@x.com", [(1, "unsubscribed")])
    assert (await lu.scan_link_unsubscribes(_link_client([again])))["processed"] == 1


async def test_resubscribed_list_is_forgotten_while_another_stays_unsubscribed():
    from app.services import link_unsubscribe as lu

    first = _sub(7, "p@x.com", [(1, "unsubscribed"), (2, "unsubscribed")])
    assert (await lu.scan_link_unsubscribes(_link_client([first])))["processed"] == 1
    back_on_2 = _sub(7, "p@x.com", [(1, "unsubscribed"), (2, "confirmed")])
    assert (await lu.scan_link_unsubscribes(_link_client([back_on_2])))["processed"] == 0
    clicked = _sub(7, "p@x.com", [(1, "unsubscribed"), (2, "unsubscribed")])
    assert (await lu.scan_link_unsubscribes(_link_client([clicked])))["processed"] == 1


def test_adding_to_a_list_through_the_app_makes_the_next_click_count(client):
    from app.routers import subscribers as subs_router
    from app.services import link_unsubscribe as lu

    first = _sub(8, "a@x.com", [(1, "unsubscribed")])
    assert asyncio.run(lu.scan_link_unsubscribes(_link_client([first])))["processed"] == 1
    with patch.object(subs_router.listmonk, "modify_list_memberships",
                      AsyncMock(return_value={"data": True})):
        assert client.put("/api/subscribers/lists", json={
            "ids": [8], "action": "add", "target_list_ids": [1], "status": "confirmed",
        }).status_code == 200
    again = _sub(8, "a@x.com", [(1, "unsubscribed")])
    assert asyncio.run(lu.scan_link_unsubscribes(_link_client([again])))["processed"] == 1


async def test_failed_lookup_of_remembered_subscribers_is_not_fatal():
    from app.services import link_unsubscribe as lu, unsubscribe_log

    await unsubscribe_log.add_link_seen({3: [1]})
    lm = _link_client([])
    lm.get_subscribers.side_effect = RuntimeError("down")
    res = await lu.scan_link_unsubscribes(lm)
    assert res["errors"] == 0
    assert unsubscribe_log.load_link_seen() == {3: {1}}


async def test_link_memory_update_applies_removals_then_additions():
    from app.services import unsubscribe_log as ul

    await ul.add_link_seen({1: [1, 2]})
    await ul.update_link_seen(add={1: [3]}, remove={1: [1, 3]})
    assert ul.load_link_seen() == {1: {2, 3}}
    await ul.forget_link_seen_lists({1: [2, 3]})
    assert ul.load_link_seen() == {1: set()}                 # known, nothing handled
    await ul.forget_link_seen_lists({99: [1]})
    assert 99 not in ul.load_link_seen()                     # never creates an entry


# ── 4 / 5. The IMAP opt-out scanner ─────────────────────────────────────────

async def _imap_optout(monkeypatch, subscriber, fail_blocklist=False):
    from app.services import imap_unsubscribe as svc
    from app.services.listmonk_client import ListMonkClient
    from tests.test_reply_filtering import _UnsubIMAP

    msg = email.message.EmailMessage()
    msg["From"] = f"Someone <{subscriber['email']}>"
    msg["Subject"] = "Re: hello"
    msg["Message-ID"] = "<optout-7@example.com>"
    msg["Date"] = "Wed, 12 Nov 2026 12:00:00 +0000"
    msg.set_content("Please remove me from your list.")
    monkeypatch.setattr(svc, "connect_imap", lambda: _UnsubIMAP(msg.as_bytes()))

    async def request(method, path, **kwargs):
        if method == "GET" and path == "/api/campaigns":
            return {"data": {"results": [SENT], "total": 1}}
        if method == "GET" and "/api/subscribers" in path:
            return {"data": {"results": [subscriber], "total": 1}}
        return {"data": {}}

    lm = ListMonkClient.__new__(ListMonkClient)
    lm._client = None
    lm._request = request
    if fail_blocklist:
        lm.blocklist_subscriber = AsyncMock(side_effect=RuntimeError("ListMonk 500"))
    return await svc.scan_and_unsubscribe(lm)


async def test_imap_opt_out_is_not_logged_again_by_the_link_scanner(monkeypatch):
    from app.services import link_unsubscribe as lu, unsubscribe_log

    # Opted out of list 1 by link long ago, then re-imported onto list 2.
    await unsubscribe_log.add_link_seen({7: [1]})
    await unsubscribe_log.mark_processed(["two@example.com"])
    subscriber = _sub(7, "two@example.com", [(1, "unsubscribed"), (2, "confirmed")])
    assert (await _imap_optout(monkeypatch, subscriber))["processed"] == 1

    after = _sub(7, "two@example.com", [(1, "unsubscribed"), (2, "unsubscribed")])
    assert (await lu.scan_link_unsubscribes(_link_client([after])))["processed"] == 0
    assert [r["source"] for r in unsubscribe_log.load_log()] == ["email"]


async def test_imap_blocklist_failure_still_records_the_opt_out(monkeypatch):
    from app.services import unsubscribe_log

    unsubscribe_log.save_settings({"blocklist_enabled": True})
    subscriber = _sub(4, "fail@example.com", [(5, "confirmed")])
    result = await _imap_optout(monkeypatch, subscriber, fail_blocklist=True)
    assert result["processed"] == 1 and result["errors"] == 0
    [record] = unsubscribe_log.load_log()
    assert record["source"] == "email" and record["lists_removed"] == [5]
    assert record["blocklisted"] is False                    # Undo must not lift it
    assert "fail@example.com" in unsubscribe_log.load_processed_emails()


# ── 6. The Edit form only acts on a status CHANGE ───────────────────────────

@pytest.mark.parametrize("before, sent, unblocked, marked", [
    ("enabled", "enabled", False, False),          # renamed an active lead
    ("blocklisted", "blocklisted", False, False),  # renamed a bounce-blocklisted lead
    ("blocklisted", "enabled", True, False),       # really unblocked
    ("enabled", "blocklisted", False, True),       # really blocklisted
])
def test_edit_form_registry_bookkeeping_follows_status_changes(client, before, sent, unblocked, marked):
    from app.routers import subscribers as subs_router
    from app.services import blocklist_registry

    current = {"id": 7, "email": "e@x.com", "status": before, "lists": []}
    with patch.object(subs_router.listmonk, "get_subscriber",
                      AsyncMock(return_value={"data": current})), \
            patch.object(subs_router.listmonk, "update_subscriber", AsyncMock(return_value={})):
        assert client.put("/api/subscribers/7", json={
            "email": "e@x.com", "name": "New name", "status": sent, "attribs": {}, "lists": [],
        }).status_code == 200
    assert (7 in blocklist_registry.load_unblocked()) is unblocked
    assert (7 in blocklist_registry.load()) is marked


# ── 7. One auto-unblock run at a time ───────────────────────────────────────

async def test_overlapping_auto_unblock_runs_do_not_act_twice():
    from app.services import auto_unblock

    state = {"status": "blocklisted"}
    sub = {"id": 1, "email": "x@x.com", "lists": [], "attribs": {}}

    class FakeLM:
        update_calls = 0

        async def paginate_all(self, *a, **k):
            await asyncio.sleep(0)
            return [dict(sub)] if state["status"] == "blocklisted" else []

        async def get_subscribers(self, *a, **k):
            return {}

        async def update_subscriber(self, sid, data):
            FakeLM.update_calls += 1
            await asyncio.sleep(0.01)
            state["status"] = data["status"]

        async def get_subscriber_bounces(self, sid):
            return {"data": []}

    lm = FakeLM()
    first, second = await asyncio.gather(auto_unblock.run_once(lm), auto_unblock.run_once(lm))
    assert FakeLM.update_calls == 1
    assert sorted([first["success"], second["success"]]) == [0, 1]


def test_scheduled_and_manual_auto_unblock_share_the_lock():
    import app.main as app_main

    for fn in (app_main.auto_unblock_loop, app_main.auto_unblock_run_now):
        assert "async with _AUTO_UNBLOCK_LOCK" in inspect.getsource(fn)
