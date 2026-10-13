"""Bounce mailbox ingestion: idempotency, DSN filtering, state persistence.

The bounce mailbox is shared with a human: DSNs get opened in webmail, so IMAP
``\\Seen`` cannot be the record of what was ingested. These tests pin down the
persisted processed-ID set that replaced it, plus the "delay notice is not a
bounce" rule.
"""

import email.message
import email.policy
import email.utils
import os
import re
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
        upper = spec.upper()
        if "HEADER.FIELDS" in upper:
            # Honour the requested field list (MESSAGE-ID plus the FROM/DATE/
            # SUBJECT the fingerprint fallback needs) like a real server does.
            fields = re.search(r"HEADER\.FIELDS \(([^)]*)\)", upper).group(1).split()
            wanted = {f.lower() for f in fields}
            header = "".join(
                f"{k}: {v}\r\n" for k, v in msg.items() if k.lower() in wanted
            ).encode()
            meta = f"1 (BODY[HEADER.FIELDS] {{{len(header)}}}".encode()
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


def test_state_id_fingerprint_differentiates_headerless_dsns():
    """Message-ID-less DSNs must not collapse onto one constant hash.

    The header peek used to fetch only MESSAGE-ID, so every fingerprint fell
    back to sha256("||") — after the first such DSN was processed, all the
    others were silently skipped forever.
    """
    a = _dsn("", "a@example.com", subject="Delivery failure #1")
    b = _dsn("", "b@example.com", subject="Delivery failure #2")
    id_a, id_b = bi._message_state_id(a), bi._message_state_id(b)
    assert id_a.startswith("fp:") and id_b.startswith("fp:")
    assert id_a != id_b


def test_state_id_is_empty_without_any_identity_headers():
    """No Message-ID and no fingerprint material -> no idempotency key."""
    msg = email.message.EmailMessage()
    assert bi._message_state_id(msg) == ""


def test_fetch_state_id_requests_fingerprint_headers():
    """The cheap header peek must fetch the fallback fingerprint fields too."""
    msg = _dsn("", "x@example.com", subject="Delivery failure")

    class _OneShotIMAP(_FakeIMAP):
        specs: list[str] = []

        def fetch(self, msg_id, spec):
            self.specs.append(spec)
            return super().fetch(msg_id, spec)

    conn = _OneShotIMAP({b"1": msg})
    state_id = bi._fetch_state_id(conn, b"1")
    upper = conn.specs[0].upper()
    for field in ("MESSAGE-ID", "FROM", "DATE", "SUBJECT"):
        assert field in upper, f"{field} missing from the header peek"
    assert state_id == bi._message_state_id(msg)
    assert state_id.startswith("fp:")


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


def test_state_file_is_not_world_readable(tmp_path, monkeypatch):
    """The state file must match its 0600 siblings in DATA_DIR."""
    import stat

    # NamedTemporaryFile honours the umask, so make it permissive to prove the
    # writer (not the environment) is what keeps the file private.
    monkeypatch.setattr(bi, "PROCESSED_FILE", tmp_path / "processed.json")
    old_umask = os.umask(0o022)
    try:
        bi.mark_processed_ids(["<secret@mail>"])
    finally:
        os.umask(old_umask)

    mode = stat.S_IMODE(bi.PROCESSED_FILE.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    # No temp files left behind by the atomic write.
    assert [p.name for p in tmp_path.iterdir()] == ["processed.json"]


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


@pytest.mark.asyncio
async def test_headerless_dsns_both_ingest_and_dedupe(monkeypatch, issuer_state):
    """Two Message-ID-less DSNs get distinct state ids, both ingest once."""
    msg1 = _dsn("", "one@example.com", subject="Delivery failure #1")
    msg2 = _dsn("", "two@example.com", subject="Delivery failure #2")
    fake_imap = _FakeIMAP({b"1": msg1, b"2": msg2})
    client = _wire(monkeypatch, fake_imap, _FakeClient(_subscriber(), _campaign_list()))

    first = await bi.ingest_bounce_mailbox(client)
    second = await bi.ingest_bounce_mailbox(client)

    assert first["ingested"] == 2
    assert {c["email"] for c in client.created} == {"one@example.com", "two@example.com"}
    processed = bi.load_processed_ids()
    assert len(processed) == 2
    assert len(set(processed)) == 2
    assert all(p.startswith("fp:") for p in processed)
    # The rescan skips both from the header peek alone — no duplicate bounces.
    assert second["ingested"] == 0
    assert second["scanned"] == 0
    assert len(client.created) == 2
    assert fake_imap.full_fetches == [b"1", b"2"]


class _HeaderFailIMAP(_FakeIMAP):
    """IMAP double whose header peek always fails (flaky connection)."""

    def fetch(self, msg_id, spec):
        if "HEADER.FIELDS" in spec.upper():
            raise OSError("connection reset by peer")
        return super().fetch(msg_id, spec)


@pytest.mark.asyncio
async def test_header_fetch_failure_leaves_message_unprocessed(monkeypatch, issuer_state):
    """No derivable idempotency key -> never create a bounce for the DSN.

    The message must stay unprocessed (and unseen) so the next scan retries it
    instead of ingesting it without dedup protection.
    """
    msg = _dsn("<flaky@mail>", "bounced@example.com")
    fake_imap = _HeaderFailIMAP({b"1": msg})
    client = _wire(monkeypatch, fake_imap, _FakeClient(_subscriber(), _campaign_list()))

    result = await bi.ingest_bounce_mailbox(client)

    assert result["ingested"] == 0
    assert result["errors"] == 1
    assert client.created == []
    assert bi.load_processed_ids() == []
    assert fake_imap.seen_flags == {}
    assert fake_imap.full_fetches == []


@pytest.mark.asyncio
async def test_processed_ids_persist_incrementally_before_seen(monkeypatch, issuer_state):
    """Handled ids are flushed in batches, not only at the end of the scan.

    A mid-scan crash used to re-ingest every already-created bounce as a
    duplicate; with incremental flushes the window is bounded by the batch,
    and persistence happens before the \\Seen marking.
    """
    monkeypatch.setattr(bi, "PROCESSED_FLUSH_BATCH", 1)
    msgs = {
        b"1": _dsn("<m1@mail>", "one@example.com", subject="Failure 1"),
        b"2": _dsn("<m2@mail>", "two@example.com", subject="Failure 2"),
    }
    fake_imap = _FakeIMAP(msgs)
    persisted_at_seen: list[list[str]] = []
    original_store = fake_imap.store

    def store(msg_id, op, flags):
        persisted_at_seen.append(bi.load_processed_ids())
        return original_store(msg_id, op, flags)

    fake_imap.store = store
    client = _wire(monkeypatch, fake_imap, _FakeClient(_subscriber(), _campaign_list()))

    result = await bi.ingest_bounce_mailbox(client)

    assert result["ingested"] == 2
    # When each message was marked \Seen, its own id was already on disk.
    assert persisted_at_seen[0] == ["<m1@mail>"]
    assert persisted_at_seen[1] == ["<m1@mail>", "<m2@mail>"]


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