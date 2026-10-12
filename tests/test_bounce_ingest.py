"""Bounce mailbox ingestion: idempotency, DSN filtering, state persistence.

The bounce mailbox is shared with a human: DSNs get opened in webmail, so IMAP
``\\Seen`` cannot be the record of what was ingested. These tests pin down the
persisted processed-ID set that replaced it, plus the "delay notice is not a
bounce" rule.
"""

import email.message
import email.policy
import email.utils
from datetime import datetime, timedelta, timezone

import pytest

from app.services import bounce_ingest as bi


# ── Test doubles ────────────────────────────────────────────────────────────

class _FakeIMAP:
    """Minimal IMAP double: date-window search, header peek, RFC822 fetch."""

    def __init__(self, messages: dict[bytes, email.message.EmailMessage]):
        self.messages = messages
        self.seen_flags: dict[bytes, str] = {}
        self.criteria: str | None = None
        self.full_fetches: list[bytes] = []

    def select(self, mailbox, readonly=False):
        return ("OK", [b"1"])

    def search(self, charset, criteria):
        self.criteria = criteria
        ids = b" ".join(self.messages.keys())
        return ("OK", [ids])

    def fetch(self, msg_id, spec):
        msg = self.messages[msg_id]
        if "MESSAGE-ID" in spec.upper():
            header = f"Message-ID: {msg.get('Message-ID')}\r\n".encode()
            meta = f"1 (BODY[HEADER.FIELDS (MESSAGE-ID)] {{{len(header)}}}".encode()
            return ("OK", [(meta, header)])
        self.full_fetches.append(msg_id)
        raw = msg.as_bytes(policy=email.policy.default)
        return ("OK", [(b"1 (RFC822)", raw)])

    def store(self, msg_id, op, flags):
        self.seen_flags[msg_id] = flags
        return ("OK", [b""])

    def logout(self):
        return ("BYE", [b""])


def _dsn(
    message_id: str,
    recipient: str,
    *,
    action: str = "failed",
    status: str = "5.1.1",
    subject: str = "Mail delivery failed: returning message to sender",
    x_headers: dict | None = None,
) -> email.message.EmailMessage:
    """Build an RFC 3464-ish DSN the ingester can parse."""
    msg = email.message.EmailMessage()
    msg["From"] = "MAILER-DAEMON@mail.example.com"
    msg["To"] = "bounces@example.com"
    msg["Subject"] = subject
    msg["Date"] = email.utils.format_datetime(
        datetime.now(timezone.utc) - timedelta(hours=2)
    )
    msg["Message-ID"] = message_id
    for key, value in (x_headers or {}).items():
        msg[key] = value
    msg.set_content(
        f"Reporting-MTA: dns; mail.example.com\n\n"
        f"Final-Recipient: rfc822; {recipient}\n"
        f"Action: {action}\n"
        f"Status: {status}\n\n"
        f"Diagnostic-Code: smtp; 550 {status} user unknown\n"
    )
    return msg


class _FakeClient:
    def __init__(self, subscriber: dict | None, campaigns: list[dict]):
        self.subscriber = subscriber
        self.campaigns = campaigns
        self.created: list[dict] = []
        self.lookups: list[str] = []

    async def get_campaigns(self, page=1, per_page=100, order_by="created_at", order="DESC"):
        return {"data": {"results": self.campaigns if page == 1 else [], "total": len(self.campaigns)}}

    async def get_subscribers(self, page, per_page, query):
        self.lookups.append(query)
        results = [self.subscriber] if self.subscriber else []
        return {"data": {"results": results, "total": len(results)}}

    async def create_bounce(self, **kwargs):
        self.created.append(kwargs)
        return {"data": True}


@pytest.fixture
def issuer_state(tmp_path, monkeypatch):
    """Point the processed-state file at a temp dir."""
    monkeypatch.setattr(bi, "PROCESSED_FILE", tmp_path / "bounce_processed.json")
    return bi.load_processed_ids()


def _wire(monkeypatch, fake_imap, fake_client, opened=False):
    monkeypatch.setattr(bi, "connect_bounce_imap_rw", lambda: fake_imap)
    monkeypatch.setattr(bi, "subscriber_opened_campaign", lambda *a, **k: _async_return(opened))
    return fake_client


async def _async_return(value):
    return value


def _campaign_list() -> list[dict]:
    return [{
        "id": 7,
        "uuid": "camp-uuid-7",
        "name": "1st Campaign",
        "created_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "lists": [{"id": 1, "name": "Default list"}],
    }]


def _subscriber() -> dict:
    return {"id": 501, "email": "bounced@example.com", "lists": [{"id": 1}]}


# ── State helpers ───────────────────────────────────────────────────────────

def test_state_id_prefers_message_id():
    msg = _dsn("<abc@mail>", "bounced@example.com")
    assert bi._message_state_id(msg) == "<abc@mail>"


def test_state_id_falls_back_to_fingerprint():
    msg = _dsn("", "bounced@example.com")
    first = bi._message_state_id(msg)
    assert first.startswith("fp:") and len(first) == 35
    assert first == bi._message_state_id(msg)


def test_mark_processed_ids_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(bi, "PROCESSED_FILE", tmp_path / "processed.json")
    bi.mark_processed_ids(["<a>", "<b>"])
    bi.mark_processed_ids(["<b>", "<c>"])
    assert bi.load_processed_ids() == ["<a>", "<b>", "<c>"]


def test_processed_ids_are_capped(monkeypatch, tmp_path):
    monkeypatch.setattr(bi, "PROCESSED_FILE", tmp_path / "processed.json")
    monkeypatch.setattr(bi, "MAX_PROCESSED_IDS", 3)
    bi.mark_processed_ids([f"<{i}>" for i in range(5)])
    # Keeps the newest entries, so a bounded file cannot resurrect old DSNs.
    assert bi.load_processed_ids() == ["<2>", "<3>", "<4>"]


def test_dsn_action_detection():
    body = "Final-Recipient: rfc822; x@y.com\nAction: delayed\nStatus: 4.0.0\n"
    assert bi._dsn_action(body) == "delayed"
    assert bi._dsn_action("no action field here") == ""


def test_recipient_from_inline_dsn_fields():
    """Bare-text DSNs carry the recipient only in a Final-Recipient line."""
    msg = _dsn("<inline@mail>", "inline-fail@example.com")
    body = bi._extract_body(msg)
    assert bi._extract_bounced_recipient(msg, body) == "inline-fail@example.com"


def test_recipient_from_multipart_delivery_status_part():
    """Standard RFC 3464 DSNs keep the fields in a container child message."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = "MAILER-DAEMON@mail.example.com"
    msg["Subject"] = "Mail delivery failed"
    msg["Message-ID"] = "<multipart@mail>"
    msg.set_content("Human readable explanation.")
    msg.make_mixed()
    dsn = EmailMessage()
    dsn.set_type("message/delivery-status")
    per_message = EmailMessage()
    per_message["Reporting-MTA"] = "dns; mail.example.com"
    per_recipient = EmailMessage()
    per_recipient["Final-Recipient"] = "rfc822; multi-fail@example.com"
    per_recipient["Action"] = "failed"
    per_recipient["Status"] = "5.1.1"
    dsn.set_payload([per_message, per_recipient])
    msg.attach(dsn)

    body = bi._extract_body(msg)
    assert bi._extract_bounced_recipient(msg, body) == "multi-fail@example.com"


# ── Ingestion behaviour ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hard_bounce_is_ingested_even_when_already_read(monkeypatch, issuer_state):
    """A DSN opened in webmail (\\Seen) must still be ingested once."""
    msg = _dsn("<read@mail>", "bounced@example.com")
    fake_imap = _FakeIMAP({b"1": msg})
    client = _wire(monkeypatch, fake_imap, _FakeClient(_subscriber(), _campaign_list()))

    result = await bi.ingest_bounce_mailbox(client)

    assert result["ingested"] == 1
    assert result["hard"] == 1
    assert client.created[0]["email"] == "bounced@example.com"
    assert client.created[0]["bounce_type"] == "hard"
    assert "SINCE" in fake_imap.criteria and "UNSEEN" not in fake_imap.criteria
    assert bi.load_processed_ids() == ["<read@mail>"]


@pytest.mark.asyncio
async def test_second_scan_does_not_re_ingest(monkeypatch, issuer_state):
    msg = _dsn("<once@mail>", "bounced@example.com")
    fake_imap = _FakeIMAP({b"1": msg})
    client = _wire(monkeypatch, fake_imap, _FakeClient(_subscriber(), _campaign_list()))

    first = await bi.ingest_bounce_mailbox(client)
    second = await bi.ingest_bounce_mailbox(client)

    assert first["ingested"] == 1
    assert second["ingested"] == 0
    assert second["scanned"] == 0
    assert len(client.created) == 1
    # The processed DSN is skipped from the header alone, without downloading.
    assert fake_imap.full_fetches == [b"1"]


@pytest.mark.asyncio
async def test_delay_notice_is_not_recorded_as_bounce(monkeypatch, issuer_state):
    msg = _dsn(
        "<delay@mail>", "bounced@example.com",
        action="delayed", status="4.0.0",
        subject="Warning: message delayed 24 hours",
    )
    fake_imap = _FakeIMAP({b"1": msg})
    client = _wire(monkeypatch, fake_imap, _FakeClient(_subscriber(), _campaign_list()))

    result = await bi.ingest_bounce_mailbox(client)

    assert result["ingested"] == 0
    assert client.created == []
    assert result["skipped_reasons"].get("not_a_bounce:delayed") == 1
    # Still marked handled, so the notice is not rescanned every hour.
    assert bi.load_processed_ids() == ["<delay@mail>"]


@pytest.mark.asyncio
async def test_failed_dsn_for_non_subscriber_is_skipped_once(monkeypatch, issuer_state):
    msg = _dsn("<stranger@mail>", "stranger@example.com")
    fake_imap = _FakeIMAP({b"1": msg})
    client = _wire(monkeypatch, fake_imap, _FakeClient(None, _campaign_list()))

    first = await bi.ingest_bounce_mailbox(client)
    second = await bi.ingest_bounce_mailbox(client)

    assert first["skipped_reasons"].get("no_subscriber") == 1
    assert client.created == []
    assert second["scanned"] == 0


@pytest.mark.asyncio
async def test_create_bounce_failure_is_retried_next_scan(monkeypatch, issuer_state):
    class _FailingClient(_FakeClient):
        async def create_bounce(self, **kwargs):
            raise RuntimeError("ListMonk unavailable")

    msg = _dsn("<retry@mail>", "bounced@example.com")
    fake_imap = _FakeIMAP({b"1": msg})
    client = _wire(monkeypatch, fake_imap, _FailingClient(_subscriber(), _campaign_list()))

    result = await bi.ingest_bounce_mailbox(client)

    assert result["errors"] == 1
    assert result["ingested"] == 0
    # Not recorded as processed, so the next scan tries again.
    assert bi.load_processed_ids() == []


# ── Unattributable DSNs: discard only when attribution was actually possible ──

class _NoCampaignClient(_FakeClient):
    """Campaign history unavailable (e.g. ListMonk unreachable at boot)."""

    def __init__(self, subscriber):
        super().__init__(subscriber, [])

    async def get_campaigns(self, page=1, per_page=100, order_by="created_at", order="DESC"):
        raise RuntimeError("ListMonk API unavailable")


@pytest.mark.asyncio
async def test_unattributable_dsn_is_retried_when_history_unavailable(monkeypatch, issuer_state):
    """A transient campaign-fetch failure must not discard the bounce forever."""
    msg = _dsn("<nohist@mail>", "bounced@example.com")
    fake_imap = _FakeIMAP({b"1": msg})
    client = _wire(monkeypatch, fake_imap, _NoCampaignClient(_subscriber()))

    result = await bi.ingest_bounce_mailbox(client)

    assert result["skipped_reasons"].get("no_campaign") == 1
    assert client.created == []
    # Left unprocessed so a later scan (with campaign history) can attribute it.
    assert bi.load_processed_ids() == []
    assert fake_imap.seen_flags == {}


@pytest.mark.asyncio
async def test_unattributable_dsn_is_discarded_when_history_is_complete(monkeypatch, issuer_state):
    """With a complete campaign list, 'no campaign' is a real answer -> handle once."""
    msg = _dsn("<old@mail>", "bounced@example.com")
    fake_imap = _FakeIMAP({b"1": msg})
    # Subscriber belongs to a list no campaign ever targeted.
    subscriber = {"id": 501, "email": "bounced@example.com", "lists": [{"id": 99}]}
    client = _wire(monkeypatch, fake_imap, _FakeClient(subscriber, _campaign_list()))

    first = await bi.ingest_bounce_mailbox(client)
    second = await bi.ingest_bounce_mailbox(client)

    assert first["skipped_reasons"].get("no_campaign") == 1
    assert bi.load_processed_ids() == ["<old@mail>"]
    assert second["scanned"] == 0