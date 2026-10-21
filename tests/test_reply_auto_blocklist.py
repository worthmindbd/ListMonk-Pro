"""Opt-in auto-blocklisting of automated replies.

The reply scanner can blocklist the ListMonk subscriber behind a newly
ingested automated reply, decided per kind (see
``imap_reply_tracker._auto_reply_kind``):

- ``auto_blocklist_machine`` — no-reply/notification senders and ticket
  systems: mailboxes no human reads.
- ``auto_blocklist_ooo``     — out-of-office notices written by a real
  (temporarily away) human; the harsher, opt-in choice.

Each test names the behaviour it pins down so a refactor that silently
reintroduces the wrong default or the wrong scope fails loudly.
"""

import pytest
from unittest.mock import patch

from tests.conftest import CSRFTestClient

from app.main import app
from app.services import reply_log as rl
from app.services.imap_reply_tracker import (
    _auto_reply_kind,
    classify_sentiment,
)


# ── Fakes ────────────────────────────────────────────────────────────────────

class _FakeLM:
    """ListMonk double resolving every sender to subscriber #42."""

    def __init__(self):
        self.blocklisted = []

    async def get_subscribers(self, page, per_page, query):
        return {"data": {"results": [{"id": 42, "email": "x@example.com"}], "total": 1}}

    async def blocklist_subscriber(self, subscriber_id):
        self.blocklisted.append(subscriber_id)
        return {"data": True}


class _BrokenBlocklistLM(_FakeLM):
    async def blocklist_subscriber(self, subscriber_id):
        raise RuntimeError("listmonk down")


class _InboxIMAP:
    """Minimal IMAP stand-in serving a fixed set of raw messages."""

    def __init__(self, raw_messages):
        self.raw_messages = raw_messages

    def select(self, *args, **kwargs):
        return "OK", [b"1"]

    def search(self, *args, **kwargs):
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.raw_messages)))]

    def fetch(self, num, spec):
        idx = int(num) - 1
        return "OK", [(num, self.raw_messages[idx])]

    def close(self):
        pass

    def logout(self):
        pass


def _raw(msg_from, subject, body, msg_id):
    return (
        f"From: {msg_from}\r\n"
        "To: me@example.com\r\n"
        f"Subject: {subject}\r\n"
        "Date: Wed, 14 Oct 2026 10:00:00 +0000\r\n"
        f"Message-ID: {msg_id}\r\n"
        "\r\n"
        f"{body}\r\n"
    ).encode()


NOREPLY_AUTO = _raw(
    "noreply@acme.com", "Votre demande a bien été reçue",
    "This is an automated message. A team member will be in touch soon.",
    "<noreply-1@example.com>",
)
TICKET_AUTO = _raw(
    "support@zendesk.com", "[Ticket #123] Request received",
    "Your request has been received. Do not reply to this email.",
    "<ticket-1@example.com>",
)
OOO_HUMAN = _raw(
    "jane@prospect.com", "Automatic reply: Re: Quick question",
    "I am out of the office until Monday with limited access to email.",
    "<ooo-1@example.com>",
)


async def _run_scan(tmp_path, monkeypatch, messages, settings_overrides=None):
    """Run a reply scan against a fake inbox with tmp-backed state files."""
    import app.services.imap_reply_tracker as T
    import app.services.bounce_ingest as bi

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "reply_log.json")
    monkeypatch.setattr(rl, "PROCESSED_FILE", tmp_path / "reply_processed.json")
    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    monkeypatch.setattr(bi, "PROCESSED_FILE", tmp_path / "bounce_processed.json")
    for key, value in (settings_overrides or {}).items():
        rl.save_settings({key: value})

    monkeypatch.setattr(T.settings, "imap_host", "imap.example.com")
    monkeypatch.setattr(T.settings, "imap_user", "me@example.com")
    monkeypatch.setattr(T.settings, "imap_pass", "secret")
    monkeypatch.setattr(T.settings, "imap_use_ssl", False)

    async def _campaigns(listmonk, cutoff):
        return [], True

    monkeypatch.setattr(T, "_fetch_campaigns_covering", _campaigns)
    monkeypatch.setattr(T, "_connect_imap", lambda: _InboxIMAP(messages))

    fake = _FakeLM()
    stats = await T._scan_and_track_replies_impl(fake, days_back=14)
    return stats, fake, rl.load_log()


# ── Kind split ───────────────────────────────────────────────────────────────

def test_auto_reply_kind_splits_machine_from_ooo():
    # Machine: explicit automated wording, ticket systems, no-reply senders.
    assert _auto_reply_kind("[Ticket #9] Request received", "Do not reply.", "") == "machine"
    assert _auto_reply_kind("Thanks", "We have received your message.", "") == "machine"
    assert _auto_reply_kind("Notification", "Weekly digest", "no-reply@acme.com") == "machine"
    # Machine wording wins when an away notice is also present: a mailbox that
    # says both things is still not read by a human.
    assert _auto_reply_kind(
        "Out of office", "This is an automated message.", "jane@x.com",
    ) == "machine"
    # OOO: away notices from a real address.
    assert _auto_reply_kind(
        "Re: Hi", "I am out of the office until Monday.", "jane@x.com",
    ) == "ooo"
    assert _auto_reply_kind("", "Estoy fuera de oficina hasta el lunes.", "jane@x.es") == "ooo"
    # Not automated at all.
    assert _auto_reply_kind("Re: Hi", "Interested, tell me more!", "jane@x.com") is None


def test_classify_sentiment_unchanged_by_kind_split():
    """The feed's sentiment badge must not move: both kinds stay auto_reply."""
    assert classify_sentiment("[Ticket #9] Received", "Do not reply.") == "auto_reply"
    assert classify_sentiment("Any subject", "Any body", "noreply@acme.com") == "auto_reply"
    assert classify_sentiment(
        "Automatic reply: Re: Hi", "Out of office until Monday.", "jane@x.com",
    ) == "auto_reply"
    # And a human reply is still untouched by the split.
    assert classify_sentiment("Re: Hi", "We are interested — send pricing!") == "positive"


# ── Scan hook ────────────────────────────────────────────────────────────────

async def test_auto_blocklist_off_by_default(tmp_path, monkeypatch):
    """Without the toggles nothing is blocked — the historical behaviour."""
    stats, fake, records = await _run_scan(
        tmp_path, monkeypatch, [NOREPLY_AUTO, OOO_HUMAN],
    )

    assert stats["auto_blocklisted"] == 0
    assert fake.blocklisted == []
    assert all("auto_blocklisted" not in r for r in records)
    assert stats["auto_reply"] == 2


async def test_machine_toggle_blocks_machine_only(tmp_path, monkeypatch):
    stats, fake, records = await _run_scan(
        tmp_path, monkeypatch, [NOREPLY_AUTO, OOO_HUMAN],
        settings_overrides={"auto_blocklist_machine": True},
    )

    assert stats["auto_blocklisted"] == 1
    assert fake.blocklisted == [42]
    blocked = [r for r in records if r.get("auto_blocklisted")]
    assert [r["from_email"] for r in blocked] == ["noreply@acme.com"]
    # The OOO record is stored untagged: the machine toggle must not reach it.
    ooo = next(r for r in records if r["from_email"] == "jane@prospect.com")
    assert "auto_blocklisted" not in ooo


async def test_ooo_toggle_blocks_ooo_only(tmp_path, monkeypatch):
    stats, fake, records = await _run_scan(
        tmp_path, monkeypatch, [NOREPLY_AUTO, OOO_HUMAN],
        settings_overrides={"auto_blocklist_ooo": True},
    )

    assert stats["auto_blocklisted"] == 1
    blocked = [r for r in records if r.get("auto_blocklisted")]
    assert [r["from_email"] for r in blocked] == ["jane@prospect.com"]
    assert fake.blocklisted == [42]


async def test_both_toggles_block_every_automated_reply(tmp_path, monkeypatch):
    stats, fake, records = await _run_scan(
        tmp_path, monkeypatch, [NOREPLY_AUTO, OOO_HUMAN],
        settings_overrides={
            "auto_blocklist_machine": True, "auto_blocklist_ooo": True,
        },
    )

    assert stats["auto_blocklisted"] == 2
    assert len(fake.blocklisted) == 2
    assert all(r.get("auto_blocklisted") for r in records)


async def test_blocklist_failure_never_loses_the_reply(tmp_path, monkeypatch):
    """A failed upstream call keeps the record in the feed, untagged."""
    import app.services.imap_reply_tracker as T

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "reply_log.json")
    monkeypatch.setattr(rl, "PROCESSED_FILE", tmp_path / "reply_processed.json")
    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    import app.services.bounce_ingest as bi
    monkeypatch.setattr(bi, "PROCESSED_FILE", tmp_path / "bounce_processed.json")
    rl.save_settings({"auto_blocklist_machine": True})

    monkeypatch.setattr(T.settings, "imap_host", "imap.example.com")
    monkeypatch.setattr(T.settings, "imap_user", "me@example.com")
    monkeypatch.setattr(T.settings, "imap_pass", "secret")
    monkeypatch.setattr(T.settings, "imap_use_ssl", False)

    async def _campaigns(listmonk, cutoff):
        return [], True

    monkeypatch.setattr(T, "_fetch_campaigns_covering", _campaigns)
    monkeypatch.setattr(T, "_connect_imap", lambda: _InboxIMAP([NOREPLY_AUTO]))

    stats = await T._scan_and_track_replies_impl(_BrokenBlocklistLM(), days_back=14)

    assert stats["auto_blocklisted"] == 0
    assert stats["new_replies"] == 1
    records = rl.load_log()
    assert len(records) == 1
    assert "auto_blocklisted" not in records[0]


async def test_same_address_blocked_once_per_scan(tmp_path, monkeypatch):
    """A chatty ticket system must not trigger one blocklist per message."""
    second = _raw(
        "noreply@acme.com", "Demande reçue",
        "We have received your message.", "<noreply-2@example.com>",
    )
    stats, fake, records = await _run_scan(
        tmp_path, monkeypatch, [NOREPLY_AUTO, second],
        settings_overrides={"auto_blocklist_machine": True},
    )

    assert stats["new_replies"] == 2
    assert stats["auto_blocklisted"] == 1
    assert fake.blocklisted == [42]


# ── Settings API ─────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.services.reply_log as rl_mod

    monkeypatch.setattr(rl_mod, "LOG_FILE", tmp_path / "reply_log.json")
    monkeypatch.setattr(rl_mod, "PROCESSED_FILE", tmp_path / "reply_processed.json")
    monkeypatch.setattr(rl_mod, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app, raise_server_exceptions=False)


def test_reply_settings_expose_auto_blocklist_defaults(client):
    body = client.get("/api/replies/settings").json()
    assert body["auto_blocklist_machine"] is False
    assert body["auto_blocklist_ooo"] is False


def test_reply_settings_roundtrip_and_whitelist(client):
    resp = client.put("/api/replies/settings", json={
        "auto_blocklist_machine": True,
        "auto_blocklist_ooo": True,
        "junk": {"big": "payload"},
    })
    assert resp.status_code == 200
    saved = client.get("/api/replies/settings").json()
    assert saved["auto_blocklist_machine"] is True
    assert saved["auto_blocklist_ooo"] is True
    assert "junk" not in saved
