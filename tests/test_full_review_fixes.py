"""Regression tests for the full-codebase review fixes.

Each test pins one verified finding from the review so a future refactor
cannot silently reintroduce it: CSV blank-header mis-mapping, oversized-cell
500s, the silent no-op campaign archive, lost unsubscribe requests on a
transient ListMonk failure, bounce dedup-state eviction/races, reply-router
input validation and locking, scheduler null/crash and lost auto-pause
tracking, rate-limiter mass reset, and defensive state-file loading.
"""

import asyncio
import email.message
import email.policy
import email.utils
import inspect
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


# ── CSV converter: blank header cells must not shift column indexes ─────────

def test_convert_csv_leading_blank_header():
    """``pandas.DataFrame.to_csv(index=True)`` emits a blank first header.

    The blank cell used to compact the header list, so the email column
    resolved onto the index-number column and every row was rejected as an
    invalid address.
    """
    from app.services.csv_converter import convert_csv

    data = b",name,email\n1,Alice,alice@example.com\n2,Bob,bob@example.com\n"
    result = convert_csv(data, "email", "name", [], auto_attributes=True)

    assert result["stats"]["converted"] == 2
    assert result["stats"]["invalid_emails"] == 0
    assert "alice@example.com" in result["csv_content"]
    assert "bob@example.com" in result["csv_content"]


def test_convert_csv_mid_file_blank_header():
    from app.services.csv_converter import convert_csv

    data = b"name,,email,city\nAlice,,alice@example.com,Oslo\n"
    result = convert_csv(data, "email", "name", [], auto_attributes=True)

    assert result["stats"]["converted"] == 1
    # The auto-promoted attribute must come from the real `city` column, not
    # from whatever the compacted index landed on.
    assert "alice@example.com" in result["csv_content"]
    assert "city" in result["csv_content"] and "Oslo" in result["csv_content"]


def test_detect_columns_hides_blank_header_but_counts_rows_correctly():
    from app.services.csv_converter import detect_columns

    data = b",name,email\n1,Alice,alice@example.com\n"
    detected = detect_columns(data)

    assert detected["columns"] == ["name", "email"]
    assert detected["suggested"]["email"] == "email"
    # With positional placeholders kept, a full-width row is not "ragged".
    assert detected["ragged_rows"] == 0

    ragged = detect_columns(b"name,email\nAlice,alice@example.com,extra\n")
    assert ragged["ragged_rows"] == 1


def test_convert_csv_oversized_field_does_not_raise():
    """A single cell beyond csv's 128 KiB field limit must convert, not 500."""
    from app.services.csv_converter import convert_csv

    big = "x" * 200_000
    data = f"name,email,bio\nAlice,alice@example.com,{big}\n".encode()
    result = convert_csv(data, "email", "name", [], auto_attributes=True)

    assert result["stats"]["converted"] == 1


# ── ListMonk client: archive must send the required body ────────────────────

@pytest.mark.asyncio
async def test_archive_campaign_sends_archive_true():
    """ListMonk binds `archive` only from the JSON body: an empty PUT answered
    200 while writing archive=false, so Archive was a silent no-op (and
    un-archived an archived campaign)."""
    from app.services.listmonk_client import ListMonkClient

    client = ListMonkClient()
    client._request = AsyncMock(return_value={"data": True})
    await client.archive_campaign(7)

    client._request.assert_awaited_once_with(
        "PUT", "/api/campaigns/7/archive", json={"archive": True},
    )


# ── Replies router: input validation and locking ─────────────────────────────

@pytest.fixture
def replies_client():
    """Authed TestClient (the middleware otherwise answers 401)."""
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from app.main import app

    with patch("app.main.verify_session", return_value=True):
        yield TestClient(app, raise_server_exceptions=False)


def test_reply_bulk_status_rejects_non_object_body(tmp_path, monkeypatch, replies_client):
    import app.services.reply_log as rl

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "reply_log.json")

    # A valid-JSON non-dict body used to raise AttributeError -> 500.
    resp = replies_client.put("/api/replies/status", json=[1, 2])
    assert resp.status_code == 400
    resp = replies_client.put("/api/replies/status", json="x")
    assert resp.status_code == 400


def test_reply_blocklist_rejects_non_object_body(tmp_path, monkeypatch, replies_client):
    import app.services.reply_log as rl

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "reply_log.json")

    resp = replies_client.post("/api/replies/blocklist", json=["abc"])
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_single_reply_status_update_holds_the_log_lock(monkeypatch):
    """The single-record status update must hold the same lock as the scanner's
    append and the bulk actions, or a stale snapshot can clobber a concurrent
    write to reply_log.json."""
    import app.services.reply_log as rl
    from app.routers import replies as replies_router

    acquired_during_write = False
    original = rl.update_reply_status

    def _spy(reply_id, new_status):
        nonlocal acquired_during_write
        acquired_during_write = replies_router._log_lock.locked()
        return original(reply_id, new_status)

    # The router imported its own binding of update_reply_status, so patch the
    # router's reference (the one actually called by the endpoint).
    monkeypatch.setattr(replies_router, "update_reply_status", _spy)

    class _FakeRequest:
        def __init__(self, payload):
            self._payload = payload

        async def json(self):
            return self._payload

    monkeypatch.setattr(
        rl, "load_log", lambda: [{"id": "rep_1", "status": "unread"}],
    )
    monkeypatch.setattr(rl, "save_log", lambda records: None)

    resp = await replies_router.update_status("rep_1", _FakeRequest({"status": "lead"}))
    assert resp["status"] == "ok"
    assert acquired_during_write, "update_status must run under _log_lock"


# ── Config: non-integer IMAP_PORT must not crash the app at import ──────────

@pytest.mark.parametrize("env_value", ["", "abc", "993a", "0", "-1"])
def test_bad_imap_port_falls_back_to_default(env_value, tmp_path):
    """`IMAP_PORT=` or a typo used to raise ValueError at import, killing the
    whole app before uvicorn could even start."""
    code = (
        "import app.config; "
        "print(app.config.settings.imap_port, app.config.settings.bounce_imap_port)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={"IMAP_PORT": env_value, "BOUNCE_IMAP_PORT": env_value,
             "DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "993 993"


# ── Auth: the limiter's overflow path must not reset locked-out buckets ─────

def test_rate_limiter_overflow_evicts_oldest_instead_of_clearing(monkeypatch):
    import app.auth as auth

    monkeypatch.setattr(auth, "_MAX_TRACKED_IPS", 3)
    monkeypatch.setattr(auth, "LOGIN_MAX_ATTEMPTS", 2)
    auth._login_failures.clear()

    try:
        # Two IPs reach the lockout threshold...
        for ip in ("ip_a", "ip_b"):
            for _ in range(2):
                auth.record_login_failure(ip)
        assert auth.is_login_rate_limited("ip_a")
        # ...then new sources push the tracker past its cap.
        auth.record_login_failure("ip_c")
        auth.record_login_failure("ip_d")

        # clear() used to wipe ip_a/ip_b here, unlocking active brute-force
        # sources; eviction must only drop the OLDEST bucket.
        assert auth._login_failures, "buckets must not be wiped"
        assert auth.is_login_rate_limited("ip_a") or auth.is_login_rate_limited("ip_b"), (
            "locked-out IPs must survive the eviction"
        )
        assert len(auth._login_failures) <= 3
    finally:
        auth._login_failures.clear()


# ── Unsubscribe log: corrupt (non-list) state file must not 500 every route ──

def test_unsubscribe_load_log_tolerates_non_list_json(tmp_path, monkeypatch):
    import app.services.unsubscribe_log as ul

    log_file = tmp_path / "unsubscribe_log.json"
    log_file.write_text(json.dumps({"oops": "dict"}), encoding="utf-8")
    monkeypatch.setattr(ul, "LOG_FILE", log_file)

    assert ul.load_log() == []

    log_file.write_text("just a string", encoding="utf-8")
    assert ul.load_log() == []


# ── Scheduler: null-valued keys and lost auto-pause tracking ────────────────

def test_load_schedule_null_keys_fall_back_to_defaults(tmp_path, monkeypatch):
    """A hand-edited schedule.json with explicit nulls used to crash every
    scheduler tick (and GET /api/scheduler) with a TypeError."""
    import app.services.campaign_scheduler as sched

    monkeypatch.setattr(sched, "SCHEDULE_FILE", tmp_path / "schedule.json")
    sched.save_schedule({
        "enabled": True,
        "timezone": None,
        "days": None,
        "auto_paused_campaigns": None,
    })

    loaded = sched.load_schedule()
    assert loaded["days"] == sched.DEFAULT_SCHEDULE["days"]
    assert loaded["auto_paused_campaigns"] == []
    assert loaded["timezone"] == sched.DEFAULT_SCHEDULE["timezone"]
    # Other keys survive.
    assert loaded["enabled"] is True

    # The tick must not raise on this schedule either.
    loaded["timezone"] = "UTC"
    assert isinstance(sched.is_within_send_window(loaded), bool)

    # An invalid timezone name also falls back instead of raising.
    sched.save_schedule({"timezone": "Not/AZone"})
    assert sched.load_schedule()["timezone"] == sched.DEFAULT_SCHEDULE["timezone"]


class _PauseClient:
    async def paginate_all(self, fn, per_page=100, **kwargs):
        status = kwargs.get("status")
        if status == "running":
            return [{"id": 5, "name": "C5", "status": "running"}]
        return []

    async def get_campaigns(self, **kwargs):
        return {"data": {"results": [], "total": 0}}

    async def change_campaign_status(self, cid, status):
        return {"data": True}


@pytest.mark.asyncio
async def test_auto_pause_is_persisted_even_when_final_save_fails(
    tmp_path, monkeypatch
):
    """The tracking record must hit disk immediately after each pause: waiting
    for the end-of-tick save meant a save failure left the campaign paused in
    ListMonk without a tracking record — never auto-resumed."""
    import app.services.campaign_scheduler as sched

    sched_file = tmp_path / "schedule.json"
    monkeypatch.setattr(sched, "SCHEDULE_FILE", sched_file)
    monkeypatch.setattr(sched, "_SCHED_LOCK", asyncio.Lock())
    monkeypatch.setattr(sched, "is_within_send_window", lambda s: False)
    sched.save_schedule({
        "enabled": True, "timezone": "UTC",
        "start_hour": 0, "start_minute": 0, "end_hour": 23, "end_minute": 59,
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "auto_paused_campaigns": [],
    })

    real_save = sched.save_schedule
    calls = {"n": 0}

    def _fail_late(schedule):
        calls["n"] += 1
        # The per-pause save is the FIRST call; only the end-of-tick save fails.
        if calls["n"] > 1:
            raise OSError("disk full")
        real_save(schedule)

    monkeypatch.setattr(sched, "save_schedule", _fail_late)

    await sched.run_scheduler_tick(_PauseClient())

    on_disk = json.loads(sched_file.read_text(encoding="utf-8"))
    assert 5 in on_disk["auto_paused_campaigns"], (
        "the pause tracking record must survive the end-of-tick save failure"
    )


# ── Bounce ingestion: own-address exclusion, dedup cap, state lock ──────────

def test_extract_bounced_recipient_excludes_both_own_addresses(monkeypatch):
    """The reply tracker ingests DSNs from the PRIMARY inbox, whose own
    address is imap_user — not the bounce user. A bare-text DSN quoting the
    sender's own address must not be recorded as the bounced recipient."""
    from app.config import settings
    from app.services.bounce_ingest import _extract_bounced_recipient

    monkeypatch.setattr(settings, "imap_user", "me@mycompany.com")
    monkeypatch.setattr(settings, "bounce_imap_user", "")

    msg = email.message.EmailMessage()
    msg["From"] = "mailer-daemon@relay.example.com"
    msg["Subject"] = "Delivery failure"

    body = (
        "Delivery failed.\n"
        "From: Me <me@mycompany.com>\n"
        "Final-Recipient: rfc822; prospect@example.com\n"
    )
    assert _extract_bounced_recipient(msg, body) == "prospect@example.com"

    # Only own addresses present -> no recipient at all (never the sender).
    own_only = "From: Me <me@mycompany.com>\nAction: failed\n"
    assert _extract_bounced_recipient(msg, own_only) is None


def test_bounce_processed_cap_exceeds_scan_window_traffic():
    """The bounce scanner re-examines the whole 30-day window every run, so a
    5000-id cap started evicting ids of DSNs still in the window and re-created
    them as duplicate bounce records."""
    from app.services import bounce_ingest

    assert bounce_ingest.MAX_PROCESSED_IDS >= 30 * 1000


def test_bounce_mark_processed_ids_is_thread_safe(tmp_path, monkeypatch):
    """The bounce scan persists batches via asyncio.to_thread while the reply
    tracker marks from the event loop: interleaved load→extend→save cycles
    used to drop a batch of ids (and re-ingest those DSNs next scan)."""
    from app.services import bounce_ingest as bi

    monkeypatch.setattr(bi, "PROCESSED_FILE", tmp_path / "bounce_processed.json")

    def worker(offset):
        for i in range(50):
            bi.mark_processed_ids([f"<m{offset}-{i}@x.com>"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))

    stored = set(bi.load_processed_ids())
    assert len(stored) == 8 * 50, "concurrent marks must never lose an id"


# ── Unsubscribe scanner: transient campaign-fetch failure must abort ────────

class _RecordingConn:
    """IMAP double that records every \\Seen marking."""

    def __init__(self):
        self.seen = []

    def select(self, mailbox, readonly=False):
        return ("OK", [b"1"])

    def search(self, charset, criteria):
        return ("OK", [b"1 2"])

    def fetch(self, num, spec):
        return ("OK", [(b"1 (RFC822)", b"")])

    def store(self, num, flags, value):
        self.seen.append(num)
        return ("OK", [b""])

    def logout(self):
        return ("BYE", [b""])


@pytest.mark.asyncio
async def test_unsubscribe_scan_aborts_when_campaign_history_unavailable(
    tmp_path, monkeypatch
):
    """A transient ListMonk failure yields an empty campaign list; proceeding
    used to match every keyword reply against it, skip them, and mark them
    \\Seen — permanent silent loss of real unsubscribe requests."""
    import app.services.imap_unsubscribe as iu
    import app.services.unsubscribe_log as ul

    monkeypatch.setattr(ul, "LOG_FILE", tmp_path / "unsubscribe_log.json")
    monkeypatch.setattr(ul, "PROCESSED_FILE", tmp_path / "unsubscribe_processed.json")
    monkeypatch.setattr(ul, "SETTINGS_FILE", tmp_path / "unsubscribe_settings.json")

    async def _failed_history(client, cutoff):
        return [], False, False

    monkeypatch.setattr(iu, "_fetch_campaign_history", _failed_history)
    conn = _RecordingConn()
    monkeypatch.setattr(iu, "connect_imap", lambda: conn)

    result = await iu.scan_and_unsubscribe(AsyncMock())

    assert result["errors"] == 1
    assert "aborted" in result["message"].lower()
    # Nothing was consumed: no message was marked seen, so the next scan
    # retries every request in the mailbox.
    assert conn.seen == []


@pytest.mark.asyncio
async def test_unsubscribe_scan_still_runs_without_any_campaigns(tmp_path, monkeypatch):
    """The abort must trigger ONLY on a failed fetch: an installation with
    genuinely zero campaigns returns ([], True, True) and scans normally."""
    import app.services.imap_unsubscribe as iu
    import app.services.unsubscribe_log as ul

    monkeypatch.setattr(ul, "LOG_FILE", tmp_path / "unsubscribe_log.json")
    monkeypatch.setattr(ul, "PROCESSED_FILE", tmp_path / "unsubscribe_processed.json")
    monkeypatch.setattr(ul, "SETTINGS_FILE", tmp_path / "unsubscribe_settings.json")

    async def _empty_history(client, cutoff):
        return [], True, True

    monkeypatch.setattr(iu, "_fetch_campaign_history", _empty_history)
    conn = _RecordingConn()
    monkeypatch.setattr(iu, "connect_imap", lambda: conn)

    result = await iu.scan_and_unsubscribe(AsyncMock())

    assert result["errors"] == 0
    assert "aborted" not in result["message"].lower()


# ── Reply tracker: English IMAP search dates ────────────────────────────────

def test_reply_scan_search_date_is_locale_independent():
    """strftime %b renders the month per LC_TIME; IMAP SEARCH SINCE requires
    English abbreviations (the same reason the other scanners use imap_date)."""
    from app.services import imap_reply_tracker as rt

    source = inspect.getsource(rt._scan_and_track_replies_impl)
    # The IMAP search criteria must be built with the English-month helper,
    # not strftime (%b remains legitimate for the human-readable debug log).
    assert "imap_date(" in source
    assert 'strftime("%d-%b' not in source
    assert "f'(SINCE \"{since}\")'" in source


# ── Reply tracker + bounce ingester share the ingest lock ───────────────────

@pytest.mark.asyncio
async def test_inbox_dsn_ingestion_holds_the_bounce_ingest_lock():
    """The check → create_bounce → mark sequence must run under _INGEST_LOCK,
    or two scanners watching one mailbox can both create the same bounce."""
    from app.services import bounce_ingest as bi
    from app.services import imap_reply_tracker as rt

    source = inspect.getsource(rt._ingest_inbox_dsn)
    assert "_INGEST_LOCK" in source

    # The lock used inside ingest is the bounce scan's module lock, so a
    # running bounce scan serialises against the reply tracker's DSN path.
    assert bi._INGEST_LOCK is not None


def test_opener_cache_sweeps_expired_entries(monkeypatch):
    import time

    from app.services import opener_cache as oc

    oc.invalidate()
    try:
        oc._cache[1] = {"old@x.com"}
        oc._fetched_at[1] = time.monotonic() - oc.TTL_SECONDS - 1
        oc._cache[2] = {"fresh@x.com"}
        oc._fetched_at[2] = time.monotonic()

        oc._sweep_expired()

        assert 1 not in oc._cache, "expired entries must be evicted"
        assert oc.is_cached(2)
        assert not oc.is_cached(1)
        assert oc.get_cached_opener_emails(1) is None
    finally:
        oc.invalidate()
