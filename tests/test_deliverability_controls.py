"""Deliverability page controls: per-setting edits and the footer unsubscribe link.

The List-Unsubscribe *header* toggle never changed the email body, which is
where the visible link lives (the campaign template). These tests pin down the
template toggle and the whitelisted per-setting endpoint that replaced the
preset-only page.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import CSRFTestClient
from app.main import app
from app.services import template_unsubscribe as tu
from app.services.deliverability_service import (
    get_deliverability_checklist,
    update_deliverability_settings,
    validate_settings_patch,
)

# Shapes taken from ListMonk's stock template and a custom cold-email one.
DEFAULT_FOOTER = '''<div class="footer">
    <p>
        <a href="{{ UnsubscribeURL }}" style="color: #888;">{{ L.T "email.unsub" }}</a>
        &nbsp;&nbsp;
        <a href="{{ MessageURL }}" style="color: #888;">{{ L.T "email.viewInBrowser" }}</a>
    </p>
</div>{{ template "content" . }}'''

COLD_FOOTER = '''<td>
  <p style="margin: 0 0 4px 0;">
    If you'd prefer not to hear from me, <a href="{{ UnsubscribeURL }}"
      style="color: #777;">unsubscribe here</a> or <a
      href="{{ UnsubscribeURL }}?manage=true">manage preferences</a>.
  </p>
  <p>Sent by Example Co, 1 Main St.</p>
</td>{{ template "content" . }}'''


# ── Body transform ──────────────────────────────────────────────────────────

def test_hide_only_the_unsubscribe_anchor_when_paragraph_has_other_links():
    hidden = tu.hide_unsubscribe_links(DEFAULT_FOOTER)
    assert tu.unsubscribe_link_state(hidden) == "off"
    # "View in browser" must survive outside the never-rendered block.
    visible = tu._HIDDEN_RE.sub("", hidden)
    assert "MessageURL" in visible
    assert "UnsubscribeURL" not in visible


def test_hide_whole_paragraph_when_it_only_holds_unsubscribe_links():
    hidden = tu.hide_unsubscribe_links(COLD_FOOTER)
    visible = tu._HIDDEN_RE.sub("", hidden)
    # No dangling "If you'd prefer not to hear from me, or ." sentence.
    assert "prefer not to hear" not in visible
    assert "Sent by Example Co" in visible
    assert '{{ template "content" . }}' in visible


@pytest.mark.parametrize("body", [DEFAULT_FOOTER, COLD_FOOTER, "Bye — {{ UnsubscribeURL }}"])
def test_hide_is_idempotent_and_show_restores_byte_for_byte(body):
    hidden = tu.hide_unsubscribe_links(body)
    assert tu.hide_unsubscribe_links(hidden) == hidden
    assert tu.show_unsubscribe_links(hidden) == body


def test_link_state_detection():
    assert tu.unsubscribe_link_state("<p>No link</p>") == "none"
    assert tu.unsubscribe_link_state(DEFAULT_FOOTER) == "on"
    hidden = tu.hide_unsubscribe_links(DEFAULT_FOOTER)
    assert tu.unsubscribe_link_state(hidden) == "off"
    assert tu.unsubscribe_link_state(hidden + '<a href="{{ UnsubscribeURL }}">x</a>') == "mixed"


# ── Template toggle ─────────────────────────────────────────────────────────

def _templates_client(templates):
    lm = MagicMock()
    lm.get_templates = AsyncMock(return_value={"data": templates})
    lm.update_template = AsyncMock(return_value={"data": True})
    return lm


@pytest.mark.asyncio
async def test_toggle_writes_only_campaign_templates_that_change():
    lm = _templates_client([
        {"id": 1, "name": "Default", "type": "campaign", "body": DEFAULT_FOOTER},
        {"id": 2, "name": "Archive", "type": "campaign", "body": "<p>archive</p>"},
        {"id": 3, "name": "Tx", "type": "tx", "subject": "s", "body": DEFAULT_FOOTER},
        {"id": 4, "name": "Visual", "type": "campaign_visual", "body": COLD_FOOTER,
         "body_source": '{"blocks": []}'},
        {"id": 5, "name": "Already off", "type": "campaign",
         "body": tu.hide_unsubscribe_links(DEFAULT_FOOTER)},
    ])

    res = await tu.set_unsubscribe_links(lm, enabled=False)

    assert sorted(res["updated"]) == [1, 4]
    assert res["unchanged"] == [5]
    assert [t["id"] for t in res["without_link"]] == [2]
    assert res["errors"] == 0
    written = {c.args[0]: c.args[1] for c in lm.update_template.await_args_list}
    assert set(written) == {1, 4}  # transactional template untouched
    assert tu.unsubscribe_link_state(written[1]["body"]) == "off"
    # The visual editor's block JSON round-trips.
    assert written[4]["body_source"] == '{"blocks": []}'
    assert written[1]["name"] == "Default" and written[1]["type"] == "campaign"


@pytest.mark.asyncio
async def test_toggle_reports_partial_failures():
    lm = _templates_client([
        {"id": 1, "name": "A", "type": "campaign", "body": DEFAULT_FOOTER},
        {"id": 2, "name": "B", "type": "campaign", "body": DEFAULT_FOOTER},
    ])
    lm.update_template = AsyncMock(side_effect=[{"data": True}, RuntimeError("boom")])

    res = await tu.set_unsubscribe_links(lm, enabled=False)
    assert res["updated"] == [1]
    assert res["errors"] == 1
    assert res["failed"][0]["id"] == 2


@pytest.mark.asyncio
async def test_toggle_on_restores_hidden_links_and_honours_template_ids():
    hidden = tu.hide_unsubscribe_links(DEFAULT_FOOTER)
    lm = _templates_client([
        {"id": 1, "name": "A", "type": "campaign", "body": hidden},
        {"id": 2, "name": "B", "type": "campaign", "body": hidden},
    ])
    res = await tu.set_unsubscribe_links(lm, enabled=True, template_ids=[2])
    assert res["updated"] == [2]
    assert lm.update_template.await_args.args[1]["body"] == DEFAULT_FOOTER


@pytest.mark.asyncio
async def test_checklist_reports_footer_link_state_from_templates():
    lm = _templates_client([
        {"id": 1, "name": "A", "type": "campaign", "body": tu.hide_unsubscribe_links(DEFAULT_FOOTER)},
    ])
    lm.get_settings = AsyncMock(return_value={"data": {"privacy.unsubscribe_header": True}})
    result = await get_deliverability_checklist(lm)
    check = next(c for c in result["checks"] if c["id"] == "unsubscribe_link")
    assert check["status"] == "warn"
    assert result["raw_settings"]["unsubscribe_link"]["state"] == "off"

    lm.get_templates = AsyncMock(return_value={"data": [
        {"id": 1, "name": "A", "type": "campaign", "body": DEFAULT_FOOTER},
    ]})
    result = await get_deliverability_checklist(lm)
    check = next(c for c in result["checks"] if c["id"] == "unsubscribe_link")
    assert check["status"] == "pass"


# ── Settings patch validation ───────────────────────────────────────────────

def test_validate_accepts_and_normalizes_whitelisted_settings():
    clean = validate_settings_patch({
        "privacy.unsubscribe_header": False,
        "app.batch_size": 50,
        "app.message_sliding_window_duration": " 1H30M ",
        "bounce.actions.hard": "blocklist",
        "upload.extensions": [".JPG", "png", "png"],
    })
    assert clean["app.message_sliding_window_duration"] == "1h30m"
    assert clean["upload.extensions"] == ["jpg", "png"]


@pytest.mark.parametrize("patch_body", [
    {},
    {"smtp": []},                               # not editable from this page
    {"privacy.disable_tracking": "true"},       # no string → bool coercion
    {"app.batch_size": True},                   # bool is not an int
    {"app.batch_size": 1.5},
    {"app.batch_size": 0},
    {"app.concurrency": 101},
    {"app.message_sliding_window_duration": "soon"},
    {"app.message_sliding_window_duration": "0m"},
    {"bounce.actions.hard": "explode"},
    {"upload.extensions": []},
    {"upload.extensions": ["html<script>"]},
])
def test_validate_rejects_bad_patches(patch_body):
    with pytest.raises(ValueError):
        validate_settings_patch(patch_body)


@pytest.mark.asyncio
async def test_hard_bounce_action_merges_into_bounce_actions():
    lm = MagicMock()
    lm.get_settings = AsyncMock(return_value={"data": {"bounce.actions": {
        "soft": {"count": 2, "action": "none"},
        "hard": {"count": 1, "action": "none"},
    }}})
    lm.apply_settings = AsyncMock(return_value={"data": True})

    await update_deliverability_settings(lm, {"bounce.actions.hard": "blocklist", "app.batch_size": 80})

    applied = lm.apply_settings.await_args.args[0]
    assert applied["bounce.actions"]["hard"] == {"count": 1, "action": "blocklist"}
    assert applied["bounce.actions"]["soft"] == {"count": 2, "action": "none"}
    assert applied["app.batch_size"] == 80
    assert "bounce.actions.hard" not in applied


# ── API ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app)


def test_settings_endpoint_rejects_unknown_keys(client):
    with patch("app.routers.deliverability.listmonk.apply_settings", new_callable=AsyncMock) as apply:
        res = client.put("/api/deliverability/settings", json={"smtp": []})
    assert res.status_code == 400
    apply.assert_not_awaited()


def test_settings_endpoint_applies_and_returns_checklist(client):
    with patch("app.routers.deliverability.listmonk.apply_settings", new_callable=AsyncMock) as apply, \
         patch("app.routers.deliverability.listmonk.get_settings", new_callable=AsyncMock) as get, \
         patch("app.routers.deliverability.listmonk.get_templates", new_callable=AsyncMock) as tpls:
        get.return_value = {"data": {"privacy.unsubscribe_header": False}}
        tpls.return_value = {"data": []}
        res = client.put("/api/deliverability/settings", json={"privacy.unsubscribe_header": False})
    assert res.status_code == 200
    apply.assert_awaited_once_with({"privacy.unsubscribe_header": False})
    assert res.json()["checklist"]["raw_settings"]["unsubscribe_header"] is False


def test_unsubscribe_link_endpoint_validates_and_reports_total_failure(client):
    res = client.put("/api/deliverability/unsubscribe-link", json={"enabled": "no"})
    assert res.status_code == 400

    with patch("app.routers.deliverability.listmonk.get_templates", new_callable=AsyncMock) as tpls, \
         patch("app.routers.deliverability.listmonk.update_template", new_callable=AsyncMock) as upd:
        tpls.return_value = {"data": [{"id": 1, "name": "A", "type": "campaign", "body": DEFAULT_FOOTER}]}
        upd.side_effect = RuntimeError("ListMonk said no")
        res = client.put("/api/deliverability/unsubscribe-link", json={"enabled": False})
    assert res.status_code == 502


# ── ListMonk restart window (settings PUT → reload → proxy 502) ─────────────

def _http_error(status):
    import httpx
    req = httpx.Request("GET", "https://lm.example.com/api/templates")
    return httpx.HTTPStatusError("bad gateway", request=req, response=httpx.Response(status, request=req))


@pytest.mark.asyncio
async def test_toggle_retries_through_listmonk_restart(monkeypatch):
    monkeypatch.setattr(tu, "RETRY_DELAYS", (0, 0, 0))
    hidden = tu.hide_unsubscribe_links(DEFAULT_FOOTER)
    lm = _templates_client([{"id": 1, "name": "A", "type": "campaign", "body": hidden}])
    lm.get_templates = AsyncMock(side_effect=[
        _http_error(502), _http_error(503),
        {"data": [{"id": 1, "name": "A", "type": "campaign", "body": hidden}]},
    ])
    lm.update_template = AsyncMock(side_effect=[_http_error(504), {"data": True}])

    res = await tu.set_unsubscribe_links(lm, enabled=True)

    assert res["updated"] == [1]
    assert res["errors"] == 0
    assert lm.update_template.await_args.args[1]["body"] == DEFAULT_FOOTER


@pytest.mark.asyncio
async def test_toggle_does_not_retry_real_rejections(monkeypatch):
    monkeypatch.setattr(tu, "RETRY_DELAYS", (0, 0, 0))
    lm = _templates_client([{"id": 1, "name": "A", "type": "campaign", "body": DEFAULT_FOOTER}])
    lm.update_template = AsyncMock(side_effect=_http_error(400))

    res = await tu.set_unsubscribe_links(lm, enabled=False)

    assert lm.update_template.await_count == 1
    assert res["failed"][0]["error"] == "ListMonk rejected the request (HTTP 400)"


def test_unreachable_listmonk_gets_a_readable_502(client, monkeypatch):
    monkeypatch.setattr(tu, "RETRY_DELAYS", (0,))
    with patch("app.routers.deliverability.listmonk.get_templates", new_callable=AsyncMock) as tpls:
        tpls.side_effect = _http_error(502)
        res = client.put("/api/deliverability/unsubscribe-link", json={"enabled": True})
    assert res.status_code == 502
    detail = res.json()["detail"]
    assert "restarting" in detail and "mozilla" not in detail
