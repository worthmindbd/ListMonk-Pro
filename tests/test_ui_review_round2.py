"""Backend pieces added with the second UI review round.

* ``POST /api/campaigns/{id}/preview`` renders unsaved body edits.
* ``POST /api/templates/preview`` renders an unsaved template body.
* ``GET /api/replies/timeline`` counts human replies per calendar day.
* Auto-unblock: on/off switch, last run and history (``auto_unblock_state``).
* CSV import can leave out addresses that fail validation.
"""

import csv
import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app import main as app_main
from app.main import app
from app.services import auto_unblock_state, reply_log
from tests.conftest import CSRFTestClient


@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app, raise_server_exceptions=False)


# ── Unsaved previews ────────────────────────────────────────────────────────

def test_campaign_draft_preview_forwards_unsaved_body(client):
    with patch("app.routers.campaigns.listmonk") as lm:
        lm.preview_campaign_draft = AsyncMock(return_value=httpx.Response(200, text="<p>Hi Sample</p>"))
        res = client.post("/api/campaigns/7/preview",
                          json={"body": "<p>Hi {{ .Subscriber.Name }}</p>", "content_type": "html", "template_id": 3})
    assert res.status_code == 200
    assert "Hi Sample" in res.text
    assert res.headers["content-security-policy"] == "sandbox"
    lm.preview_campaign_draft.assert_awaited_once_with(7, "<p>Hi {{ .Subscriber.Name }}</p>", "html", 3)


@pytest.mark.parametrize("payload", [
    {"body": 5},
    {"body": "x", "content_type": "exe"},
    {"body": "x", "template_id": "3"},
    {"body": "x", "template_id": True},
])
def test_campaign_draft_preview_validates_input(client, payload):
    with patch("app.routers.campaigns.listmonk") as lm:
        lm.preview_campaign_draft = AsyncMock()
        assert client.post("/api/campaigns/7/preview", json=payload).status_code == 400
        lm.preview_campaign_draft.assert_not_awaited()


def test_template_body_preview_is_rendered_and_sandboxed(client):
    with patch("app.routers.templates.listmonk") as lm:
        lm.preview_template_body = AsyncMock(return_value=httpx.Response(200, text="<b>rendered</b>"))
        res = client.post("/api/templates/preview", json={"body": '{{ template "content" . }}'})
    assert res.status_code == 200
    assert res.text == "<b>rendered</b>"
    assert res.headers["content-security-policy"] == "sandbox"
    lm.preview_template_body.assert_awaited_once_with('{{ template "content" . }}', "campaign")


def test_template_body_preview_rejects_bad_input(client):
    assert client.post("/api/templates/preview", json={"body": None}).status_code == 400
    assert client.post("/api/templates/preview", json={"body": "x", "type": "nope"}).status_code == 400


# ── Replies per day ─────────────────────────────────────────────────────────

def _reply(cid, date, sentiment="neutral", mailbox=None):
    from app.config import settings
    return {"id": f"r{cid}{date}", "campaign_id": cid, "date": date, "sentiment": sentiment,
            "mailbox": settings.imap_user if mailbox is None else mailbox}


def test_reply_timeline_counts_human_campaign_replies_per_day(client):
    reply_log.save_log([
        _reply(1, "Wed, 04 Nov 2026 10:00:00 +0000"),
        _reply(1, "Wed, 04 Nov 2026 23:30:00 +0000"),       # Nov 5 in Dhaka
        _reply(1, "Thu, 05 Nov 2026 09:00:00 +0000", "positive"),
        _reply(1, "Thu, 05 Nov 2026 09:00:00 +0000", "auto_reply"),   # automated: not counted
        _reply(2, "Thu, 05 Nov 2026 09:00:00 +0000"),       # other campaign
        _reply(1, "Thu, 05 Nov 2026 09:00:00 +0000", mailbox="old@inbox"),  # other mailbox
        _reply(1, "not a date"),
    ])
    res = client.get("/api/replies/timeline?campaign_id=1")
    assert res.json()["data"] == [{"date": "2026-11-04", "count": 2}, {"date": "2026-11-05", "count": 1}]
    res = client.get("/api/replies/timeline?campaign_id=1&tz=Asia/Dhaka&from_date=2026-11-05")
    assert res.json()["data"] == [{"date": "2026-11-05", "count": 2}]
    assert len(client.get("/api/replies/timeline").json()["data"]) == 2


def test_reply_timeline_rejects_bad_parameters(client):
    assert client.get("/api/replies/timeline?tz=Mars/Base").status_code == 400
    assert client.get("/api/replies/timeline?from_date=2026-13-01").status_code == 400


# ── Auto-unblock switch and history ─────────────────────────────────────────

def test_auto_unblock_state_defaults_on_and_records_runs():
    assert auto_unblock_state.load_state() == {"enabled": True, "last_run": None, "history": []}


async def test_record_run_keeps_newest_first_and_caps(monkeypatch):
    monkeypatch.setattr(auto_unblock_state, "HISTORY_LIMIT", 3)
    t1 = datetime(2026, 11, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 2, tzinfo=timezone.utc)
    await auto_unblock_state.record_run({"success": 2, "unblocked": ["a@x.com", "b@x.com"]}, "schedule", now=t1)
    await auto_unblock_state.record_run({"success": 2, "failed": 1, "unblocked": ["c@x.com", "d@x.com"]}, "manual", now=t2)
    state = auto_unblock_state.load_state()
    assert [h["email"] for h in state["history"]] == ["c@x.com", "d@x.com", "a@x.com"]
    assert state["last_run"]["trigger"] == "manual"
    assert state["last_run"]["failed"] == 1


async def test_a_failed_run_is_recorded_with_its_error():
    await auto_unblock_state.record_run(None, "schedule", error="ListMonk down")
    last = auto_unblock_state.load_state()["last_run"]
    assert last["error"] == "ListMonk down"
    assert last["success"] == 0


def test_settings_endpoint_is_strict(client):
    assert client.put("/api/auto-unblock/settings", json={"enabled": "no"}).status_code == 400
    assert client.put("/api/auto-unblock/settings", json={"enabled": False, "x": 1}).status_code == 400
    res = client.put("/api/auto-unblock/settings", json={"enabled": False})
    assert res.status_code == 200 and res.json() == {"enabled": False}
    assert auto_unblock_state.load_state()["enabled"] is False


def test_status_reports_switch_last_run_and_history(client):
    with patch("app.main.find_blocklisted_engaged", AsyncMock(return_value=[{"id": 1}])):
        data = client.get("/api/auto-unblock/status").json()
    assert data["enabled"] is True
    assert data["blocklisted_engaged"] == 1
    assert data["last_run"] is None and data["history"] == []


def test_manual_run_is_recorded(client):
    sub = {"id": 1, "email": "a@x.com"}
    result = {"success": 1, "failed": 0, "bounces_deleted": 1, "unblocked": ["a@x.com"]}
    with patch("app.main.find_blocklisted_engaged", AsyncMock(return_value=[sub])), \
         patch("app.main.unblock_subscribers", AsyncMock(return_value=result)):
        assert client.post("/api/auto-unblock/run").status_code == 200
    state = auto_unblock_state.load_state()
    assert state["last_run"]["trigger"] == "manual"
    assert state["history"][0]["email"] == "a@x.com"


async def test_background_loop_skips_while_switched_off(monkeypatch):
    await auto_unblock_state.set_enabled(False)
    finder = AsyncMock(return_value=[])
    monkeypatch.setattr(app_main, "find_blocklisted_engaged", finder)

    async def stop(_):
        raise RuntimeError("stop")
    monkeypatch.setattr(app_main.asyncio, "sleep", stop)
    with pytest.raises(RuntimeError):
        await app_main.auto_unblock_loop()
    finder.assert_not_awaited()
    assert auto_unblock_state.load_state()["last_run"] is None


# ── Validate before import ──────────────────────────────────────────────────

async def _fake_validate_many(emails):
    out = []
    for e in emails:
        e = e.lower()
        verdict = {"dead@nowhere.invalid": "invalid", "info@acme.com": "risky",
                   "x@slowdns.com": "unknown"}.get(e, "valid")
        out.append({"email": e, "verdict": verdict, "reasons": ["no_domain"] if verdict == "invalid" else [],
                    "detail": "Domain does not exist" if verdict == "invalid" else ""})
    return out


def test_import_leaves_out_invalid_addresses_only(client):
    csv_data = (b"Email,Company\nalice@example.com,A\ndead@nowhere.invalid,B\n"
                b"info@acme.com,C\nx@slowdns.com,D\n")
    lm = AsyncMock()
    lm.import_subscribers = AsyncMock(return_value={"data": {}})
    with patch("app.routers.converter.listmonk", lm), \
         patch("app.routers.converter.email_validation.validate_many", _fake_validate_many):
        res = client.post(
            "/api/converter/convert-and-import",
            files={"file": ("t.csv", csv_data, "text/csv")},
            data={"email_column": "Email", "attribute_mode": "auto", "list_ids": "[1]",
                  "validate_emails": "true"},
        )
    assert res.status_code == 200
    v = res.json()["validation"]
    assert (v["checked"], v["kept"], v["removed"], v["risky"], v["unknown"]) == (4, 3, 1, 1, 1)
    assert v["removed_sample"] == [{"email": "dead@nowhere.invalid", "reason": "Domain does not exist"}]
    sent = lm.import_subscribers.await_args.args[0].decode()
    emails = [r["email"] for r in csv.DictReader(io.StringIO(sent))]
    assert emails == ["alice@example.com", "info@acme.com", "x@slowdns.com"]


def test_import_without_validation_is_unchanged(client):
    lm = AsyncMock()
    lm.import_subscribers = AsyncMock(return_value={"data": {}})
    with patch("app.routers.converter.listmonk", lm), \
         patch("app.routers.converter.email_validation.validate_many", AsyncMock()) as vm:
        res = client.post(
            "/api/converter/convert-and-import",
            files={"file": ("t.csv", b"Email\ndead@nowhere.invalid\n", "text/csv")},
            data={"email_column": "Email", "attribute_mode": "auto", "list_ids": "[1]"},
        )
    assert res.status_code == 200
    assert res.json()["validation"] is None
    vm.assert_not_awaited()


def test_import_refuses_when_every_address_is_invalid(client):
    lm = AsyncMock()
    with patch("app.routers.converter.listmonk", lm), \
         patch("app.routers.converter.email_validation.validate_many", _fake_validate_many):
        res = client.post(
            "/api/converter/convert-and-import",
            files={"file": ("t.csv", b"Email\ndead@nowhere.invalid\n", "text/csv")},
            data={"email_column": "Email", "attribute_mode": "auto", "list_ids": "[1]",
                  "validate_emails": "true"},
        )
    assert res.status_code == 400
    lm.import_subscribers.assert_not_awaited()


# ── Select chevron (light theme tiled it on focus, Deliverability lost it) ──

def _css_rule(css: str, selector: str) -> str:
    import re
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, selector
    return m.group(1)


def test_select_rules_never_use_the_background_shorthand():
    """A `background:` shorthand on a rule that matches a <select> resets the
    chevron's background-image / -repeat: focused (open) selects tiled the
    arrow in the light theme, the Deliverability selects had none at all."""
    from pathlib import Path
    css = (Path(__file__).resolve().parent.parent / "static/css/style.css").read_text(encoding="utf-8")
    for selector in ("input:focus, select:focus, textarea:focus", ".deliv-field .deliv-input"):
        body = _css_rule(css, selector)
        assert "background:" not in body.replace("background-color:", ""), selector
    deliv_select = _css_rule(css, ".deliv-field select.deliv-input")
    assert "padding-right" in deliv_select
