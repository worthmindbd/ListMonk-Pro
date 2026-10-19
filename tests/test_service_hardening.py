"""Service hardening regressions: IMAP socket timeouts, scanner dedup
fallbacks, CSV formula-injection defusing, bounded processed-state files,
and defensive settings loading.

Each test pins one verified review finding so a future refactor cannot
silently reintroduce it.
"""

import csv
import email.message
import email.policy
import email.utils
import inspect
import io
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest


# ── 1. IMAP connections must carry a socket timeout ──────────────────────────

def test_env_int_validation(monkeypatch):
    """Bad IMAP_TIMEOUT values fall back to the default instead of crashing
    at import time or creating a near-zero (useless) timeout."""
    from app.config import _env_int

    monkeypatch.delenv("LM_TEST_INT", raising=False)
    assert _env_int("LM_TEST_INT", 60, 5) == 60
    monkeypatch.setenv("LM_TEST_INT", "30")
    assert _env_int("LM_TEST_INT", 60, 5) == 30
    monkeypatch.setenv("LM_TEST_INT", "2")       # below the minimum
    assert _env_int("LM_TEST_INT", 60, 5) == 60
    monkeypatch.setenv("LM_TEST_INT", "abc")     # unparsable
    assert _env_int("LM_TEST_INT", 60, 5) == 60
    monkeypatch.setenv("LM_TEST_INT", "  ")      # blank counts as unset
    assert _env_int("LM_TEST_INT", 60, 5) == 60


def test_settings_imap_timeout_is_sane():
    from app.config import settings

    assert isinstance(settings.imap_timeout, int)
    assert settings.imap_timeout >= 5


def test_all_imap_connectors_pass_socket_timeout(monkeypatch):
    """Every IMAP4/IMAP4_SSL construction must set ``timeout=``.

    A hung mail server otherwise blocks a worker thread forever AND holds
    the module scan lock, silently killing the pipeline until restart.
    """
    import imaplib
    from app.config import settings
    from app.services import bounce_ingest, imap_reply_tracker, imap_unsubscribe

    captured: list = []

    class _FakeConn:
        def __init__(self, host, port, timeout=None):
            captured.append(timeout)

        def login(self, user, password):
            return ("OK", [b""])

    monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeConn)
    monkeypatch.setattr(imaplib, "IMAP4", _FakeConn)
    monkeypatch.setattr(settings, "imap_host", "mail.example.com")
    monkeypatch.setattr(settings, "imap_user", "me@mycompany.com")
    monkeypatch.setattr(settings, "imap_pass", "secret")
    monkeypatch.setattr(settings, "imap_use_ssl", True)
    monkeypatch.setattr(settings, "bounce_imap_host", "mail.example.com")
    monkeypatch.setattr(settings, "bounce_imap_user", "bounces@mycompany.com")
    monkeypatch.setattr(settings, "bounce_imap_pass", "secret")
    monkeypatch.setattr(settings, "bounce_imap_use_ssl", True)

    assert imap_unsubscribe.connect_imap() is not None
    assert imap_reply_tracker._connect_imap() is not None
    assert bounce_ingest.connect_bounce_imap_rw() is not None

    assert captured == [settings.imap_timeout] * 3


# ── 4. Reply tracker dedup for messages without a Message-ID ─────────────────

@pytest.fixture
def reply_state(tmp_path, monkeypatch):
    """Redirect reply state files into tmp and fake the IMAP configuration."""
    import app.services.reply_log as rl
    from app.config import settings

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "reply_log.json")
    monkeypatch.setattr(rl, "PROCESSED_FILE", tmp_path / "reply_processed.json")
    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    monkeypatch.setattr(settings, "imap_host", "mail.example.com")
    monkeypatch.setattr(settings, "imap_user", "me@mycompany.com")
    monkeypatch.setattr(settings, "imap_pass", "secret")
    return rl


class _FakeMail:
    """IMAP double for the reply tracker: full fetch of every message."""

    def __init__(self, messages):
        self.raw = [m.as_bytes(policy=email.policy.default) for m in messages]
        self.nums = [str(i + 1).encode() for i in range(len(messages))]

    def select(self, mailbox, readonly=False):
        return ("OK", [b"1"])

    def search(self, charset, criteria):
        return ("OK", [b" ".join(self.nums)])

    def fetch(self, num, spec):
        idx = self.nums.index(num)
        return ("OK", [(b"1 (RFC822)", self.raw[idx])])

    def close(self):
        return ("OK", [b""])

    def logout(self):
        return ("BYE", [b""])


def _reply(from_addr, subject, body, *, message_id=None, to="me@mycompany.com"):
    msg = email.message.EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.format_datetime(datetime.now(timezone.utc))
    if message_id:
        msg["Message-ID"] = message_id
    msg.set_content(body)
    return msg


def _fake_listmonk():
    lm = AsyncMock()
    lm.get_campaigns = AsyncMock(return_value={"data": {"results": [], "total": 0}})
    return lm


@pytest.mark.asyncio
async def test_reply_without_message_id_recorded_once(reply_state, monkeypatch):
    """A header-less reply must not be re-recorded on every 15-minute scan.

    The scanner used to dedup on Message-ID only, so messages without one
    were logged again on every run across the whole 14-day window. The
    From|Date|Subject fingerprint (same scheme as the bounce ingester) fixes
    the processed-set check and the persistence.
    """
    import app.services.imap_reply_tracker as rt

    msg = _reply("prospect@example.com", "Re: Quick question", "Sounds good, let's talk.")
    fake_mail = _FakeMail([msg])
    monkeypatch.setattr(rt, "_connect_imap", lambda: fake_mail)

    first = await rt.scan_and_track_replies(_fake_listmonk())
    second = await rt.scan_and_track_replies(_fake_listmonk())

    assert first["new_replies"] == 1
    assert second["new_replies"] == 0
    records = reply_state.load_log()
    assert len(records) == 1
    assert records[0]["from_email"] == "prospect@example.com"
    # The persisted dedup key is the fingerprint, not an empty Message-ID.
    processed = reply_state.load_processed_message_ids()
    assert processed and all(p.startswith("fp:") for p in processed)


@pytest.mark.asyncio
async def test_own_outgoing_mail_without_message_id_marked_processed(reply_state, monkeypatch):
    """The 'own outgoing mail' skip path must persist the fingerprint too,
    or our own sent messages are re-examined on every scan."""
    import app.services.imap_reply_tracker as rt

    msg = _reply(
        "me@mycompany.com", "Quick question", "Hello there",
        to="prospect@example.com",
    )
    fake_mail = _FakeMail([msg])
    monkeypatch.setattr(rt, "_connect_imap", lambda: fake_mail)

    first = await rt.scan_and_track_replies(_fake_listmonk())
    second = await rt.scan_and_track_replies(_fake_listmonk())

    assert first["new_replies"] == 0
    assert second["new_replies"] == 0
    assert reply_state.load_log() == []
    processed = reply_state.load_processed_message_ids()
    assert processed and all(p.startswith("fp:") for p in processed)


# ── 6. CSV exports must defuse formula injection ─────────────────────────────

def test_sanitize_cell_defuses_formula_prefixes():
    from app.services.export_service import _sanitize_cell

    assert _sanitize_cell("=cmd|'/C calc'!A0") == "'=cmd|'/C calc'!A0"
    assert _sanitize_cell("+1 (555) 000-0000") == "'+1 (555) 000-0000"
    assert _sanitize_cell("-2+3+cmd|'/C calc'!A0") == "'-2+3+cmd|'/C calc'!A0"
    assert _sanitize_cell("@SUM(A1:A9)") == "'@SUM(A1:A9)"
    assert _sanitize_cell("\t=1+1") == "'\t=1+1"
    assert _sanitize_cell("\r=1+1") == "'\r=1+1"
    # Benign cells and non-string values pass through untouched.
    assert _sanitize_cell("Alice") == "Alice"
    assert _sanitize_cell("a@x.com") == "a@x.com"
    assert _sanitize_cell("") == ""
    assert _sanitize_cell(-5) == -5
    assert _sanitize_cell(None) is None


def test_dict_list_to_csv_sanitizer():
    from app.services.export_service import dict_list_to_csv

    rows = [{
        "email": "a@x.com",
        "name": '=HYPERLINK("http://evil")',
        "attrs": {"note": "+danger"},
    }]
    out = "".join(dict_list_to_csv(rows, ["email", "name", "attrs"]))
    parsed = list(csv.DictReader(io.StringIO(out)))
    assert parsed[0]["email"] == "a@x.com"
    assert parsed[0]["name"] == "'=HYPERLINK(\"http://evil\")"
    # A JSON-dumped cell starts with "{" — not a formula prefix — so the
    # flattened value passes through unchanged.
    assert parsed[0]["attrs"] == '{"note": "+danger"}'


@pytest.mark.asyncio
async def test_streaming_csv_sanitizer():
    from app.services.export_service import aiter_dicts_to_csv

    async def rows():
        yield {"id": 1, "note": "@mention =formula"}
        yield {"id": -5, "note": "plain"}

    chunks = [c async for c in aiter_dicts_to_csv(rows(), ["id", "note"])]
    parsed = list(csv.DictReader(io.StringIO("".join(chunks))))
    assert parsed[0]["note"] == "'@mention =formula"
    assert parsed[1]["note"] == "plain"
    assert parsed[1]["id"] == "-5", "numeric cells must not gain a quote prefix"


# ── 7. Processed-state files must be bounded ─────────────────────────────────

async def test_reply_processed_ids_are_capped(tmp_path, monkeypatch):
    """reply_processed.json mirrors the bounce_processed.json cap: the newest
    MAX_PROCESSED_IDS entries win, so the file cannot grow forever."""
    import app.services.reply_log as rl

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "log.json")
    monkeypatch.setattr(rl, "PROCESSED_FILE", tmp_path / "processed.json")
    monkeypatch.setattr(rl, "MAX_PROCESSED_IDS", 3)

    await rl.mark_processed_message_ids([f"<{i}>" for i in range(5)])
    stored = json.loads((tmp_path / "processed.json").read_text(encoding="utf-8"))
    assert stored == ["<2>", "<3>", "<4>"]
    assert rl.load_processed_message_ids() == {"<2>", "<3>", "<4>"}

    # A newer entry evicts the oldest, keeping recency (not alphabetical) order.
    await rl.mark_processed_message_ids(["<5>"])
    stored = json.loads((tmp_path / "processed.json").read_text(encoding="utf-8"))
    assert stored == ["<3>", "<4>", "<5>"]


def test_unsubscribe_processed_emails_are_capped(tmp_path, monkeypatch):
    import app.services.unsubscribe_log as ul

    monkeypatch.setattr(ul, "LOG_FILE", tmp_path / "log.json")
    monkeypatch.setattr(ul, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(ul, "PROCESSED_FILE", tmp_path / "processed.json")
    monkeypatch.setattr(ul, "MAX_PROCESSED_EMAILS", 3)

    ul.save_processed_emails({"a@x.com", "b@x.com"})
    ul.save_processed_emails({"a@x.com", "b@x.com", "c@x.com"})
    ul.save_processed_emails({"a@x.com", "b@x.com", "c@x.com", "d@x.com"})

    stored = json.loads((tmp_path / "processed.json").read_text(encoding="utf-8"))
    # Known entries keep their stored (oldest-first) order; the cap trims the
    # oldest rather than an arbitrary alphabetical slice.
    assert stored == ["b@x.com", "c@x.com", "d@x.com"]
    assert ul.load_processed_emails() == {"b@x.com", "c@x.com", "d@x.com"}


def test_state_file_reads_are_explicitly_utf8():
    """State files are written with ensure_ascii=False; every read must pin
    UTF-8 so non-ASCII names cannot break on a non-UTF-8 container locale."""
    from app.services import app_settings, campaign_scheduler, unsubscribe_log

    checked = [
        (unsubscribe_log, ["load_log", "load_processed_emails", "load_settings",
                           "_load_processed_list"]),
        (campaign_scheduler, ["load_schedule"]),
        (app_settings, ["load_settings"]),
    ]
    for module, fn_names in checked:
        for name in fn_names:
            src = inspect.getsource(getattr(module, name))
            assert "encoding=" in src, (
                f"{module.__name__}.{name} must read with an explicit encoding"
            )


# ── 8. app_settings must survive a non-object state file ─────────────────────

def test_app_settings_non_object_file_falls_back_to_defaults(tmp_path, monkeypatch):
    """A JSON file holding a list/scalar used to raise TypeError inside
    ``{**defaults, **data}`` → 500 on GET /api/app-settings, which gates
    onboarding. It must fall back to the defaults instead."""
    import app.services.app_settings as app_settings

    state_file = tmp_path / "app_settings.json"
    monkeypatch.setattr(app_settings, "APP_SETTINGS_FILE", state_file)

    for bad in ("[1, 2]", '"newsletter"', "42", "null"):
        state_file.write_text(bad, encoding="utf-8")
        assert app_settings.load_settings() == {
            "mode": "newsletter", "mode_selected": False,
        }, f"non-object payload {bad} must not trap the dashboard"

    # A valid object still round-trips (and an invalid mode is normalised).
    state_file.write_text('{"mode": "cold_email", "mode_selected": true}', encoding="utf-8")
    assert app_settings.load_settings() == {"mode": "cold_email", "mode_selected": True}
    state_file.write_text('{"mode": "wat", "mode_selected": true}', encoding="utf-8")
    assert app_settings.load_settings()["mode"] == "newsletter"
