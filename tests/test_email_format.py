"""Deliverability page: per-campaign HTML design ↔ plain text switch."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import CSRFTestClient
from app.main import app
from app.services import email_format as ef
from app.services import template_unsubscribe as tu

# Trimmed shape of a real design template: MSO comments, a stylesheet, a logo,
# layout tables, attribute copy relying on `white-space: pre-line`, a styled
# signature and an unsubscribe footer.
DESIGN = '''<!DOCTYPE html>
<html><head><title>{{ if .Subscriber.Attribs.email_1_subject }}{{ .Subscriber.Attribs.email_1_subject }}{{ else }}Hi{{ end }}</title>
<!--[if mso]><style>td { font-family: Arial; }</style><![endif]-->
<style>.email-content { white-space: pre-line; }</style></head>
<body style="background:#f4f4f5">
  <table width="100%"><tr><td>
    <img src="https://lm.example.com/uploads/logo.png" alt="Brand" width="170">
    <div class="email-content">
      {{ if .Subscriber.Attribs.email_1_body }}{{ .Subscriber.Attribs.email_1_body }}{{ else }}{{ template
      "content" . }}{{ end }}
    </div>
    <table><tr><td><p>Best regards,<br>
      <span style="color:#111">Kawsar Mahmud</span><br>
      <span>CEO &amp; Founder</span></p></td></tr></table>
    <table><tr><td><p>If you'd prefer not to hear from me, <a href="{{ UnsubscribeURL }}"
      style="color:#777">unsubscribe here</a>.</p></td></tr></table>
  </td></tr></table>
  <div class="gutter">&nbsp;{{ TrackView }}</div>
</body></html>'''

HANDMADE_PLAIN = '<div dir="ltr">{{ template "content" . }}<br>\nBest,<br>\nK</div>'


# ── Conversion ──────────────────────────────────────────────────────────────

def test_classify_template():
    assert ef.classify_template(DESIGN) == "html"
    assert ef.classify_template(HANDMADE_PLAIN) == "plain"
    assert ef.classify_template(ef.derive_plain_html(DESIGN, 5)) == "plain"


def test_plain_twin_keeps_text_links_and_content_but_no_design():
    twin = ef.derive_plain_html(DESIGN, 5)
    assert "{{/* lmp:plain-of=5 */}}" in twin
    for gone in ("<img", "<table", "<style", "logo.png", "TrackView", "<!--", "<title"):
        assert gone not in twin
    # ListMonk's validator wants the canonical tag, even though the source
    # wrapped it across two lines.
    assert ef.CONTENT_TAG in twin
    assert '<a href="{{ UnsubscribeURL }}">unsubscribe here</a>' in twin
    assert "Best regards,<br>\nKawsar Mahmud<br>\nCEO &amp; Founder<br>" in twin
    # Attribute copy gets explicit breaks instead of CSS pre-line.
    assert 'splitList "\\n" (toString .)' in twin
    assert "{{ if $i }}<br>" in twin


def test_plain_twin_preserves_hidden_footer_link():
    hidden = tu.hide_unsubscribe_links(DESIGN)
    twin = ef.derive_plain_html(hidden, 5)
    assert tu.unsubscribe_link_state(twin) == "off"
    assert tu.unsubscribe_link_state(tu.show_unsubscribe_links(twin)) == "on"


def test_derived_text_part_is_safe_plain_text():
    text = ef.derive_text(DESIGN)
    # The altbody is its own template: a content tag would recurse into it.
    assert "template" not in text
    assert "<" not in text.replace("<br>", "")
    assert "{{ with .Subscriber.Attribs.email_1_body }}{{ Safe (toString .) }}{{ end }}" in text
    # ListMonk only resolves its link helpers in the bare shorthand form.
    assert "unsubscribe here ({{ UnsubscribeURL }})" in text
    assert "Safe (toString (UnsubscribeURL" not in text
    assert "CEO & Founder" in text  # entities decoded in a text part


def test_derived_text_inlines_campaign_body_for_content():
    text = ef.derive_text(HANDMADE_PLAIN, "<p>Hello <b>there</b></p><p>Second</p>", "richtext")
    assert text.startswith("Hello there\n\nSecond\n")
    assert "Best,\nK" in text


# ── Switching ───────────────────────────────────────────────────────────────

def _client(campaign: dict, templates: list[dict]):
    lm = MagicMock()
    lm.get_campaign = AsyncMock(return_value={"data": campaign})
    lm.get_templates = AsyncMock(return_value={"data": templates})
    lm.update_campaign = AsyncMock(return_value={"data": {}})
    lm.update_template = AsyncMock(return_value={"data": {}})
    lm.create_template = AsyncMock(return_value={"data": {"id": 99}})
    lm.preview_campaign = AsyncMock(return_value=MagicMock())
    lm.get_campaigns = AsyncMock(return_value={"data": {"results": [campaign]}})
    return lm


def _camp(**kw):
    base = {"id": 6, "name": "Leads", "status": "paused", "content_type": "richtext", "template_id": 5,
            "altbody": None, "attribs": {}, "body": "", "lists": [{"id": 4}, {"id": 1}], "media": []}
    return {**base, **kw}


DESIGN_TPL = {"id": 5, "name": "seq1", "type": "campaign", "body": DESIGN, "is_default": False}


@pytest.mark.asyncio
async def test_html_to_plain_creates_twin_and_remembers_html_setup():
    lm = _client(_camp(), [DESIGN_TPL])
    res = await ef.set_email_format(lm, 6, "plain")

    assert res == {"id": 6, "format": "plain", "template_id": 99, "template_name": "seq1 · plain", "changed": True}
    created = lm.create_template.await_args.args[0]
    assert created["name"] == "seq1 · plain" and "lmp:plain-of=5" in created["body"]
    payload = lm.update_campaign.await_args.args[1]
    assert payload["template_id"] == 99
    assert payload["lists"] == [4, 1] and payload["media"] == []
    assert "Safe (toString .)" in payload["altbody"]
    memory = payload["attribs"]["lmp_email_format"]
    assert memory["html"] == {"template_id": 5, "altbody": None}
    assert memory["plain"]["template_id"] == 99
    lm.preview_campaign.assert_awaited_once_with(6)


@pytest.mark.asyncio
async def test_existing_twin_is_regenerated_not_duplicated():
    stale = {"id": 12, "name": "seq1 · plain", "type": "campaign",
             "body": '{{/* lmp:plain-of=5 */}}<div>old {{ template "content" . }}</div>'}
    lm = _client(_camp(), [DESIGN_TPL, stale])
    res = await ef.set_email_format(lm, 6, "plain")
    assert res["template_id"] == 12
    lm.create_template.assert_not_awaited()
    assert lm.update_template.await_args.args[0] == 12


@pytest.mark.asyncio
async def test_plain_to_html_restores_remembered_setup():
    handmade = {"id": 8, "name": "Cold plain", "type": "campaign", "body": HANDMADE_PLAIN}
    memory = {"html": {"template_id": 5, "altbody": None}, "plain": {"template_id": 8, "altbody": "hand text"}}
    lm = _client(_camp(template_id=8, altbody="hand text", attribs={"lmp_email_format": memory, "other": 1}),
                 [DESIGN_TPL, handmade])

    res = await ef.set_email_format(lm, 6, "html")
    assert res["template_id"] == 5 and res["format"] == "html"
    payload = lm.update_campaign.await_args.args[1]
    assert payload["altbody"] == ""  # remembered "no text part" → NULL in ListMonk
    assert payload["attribs"]["other"] == 1  # unrelated attribs survive
    assert payload["attribs"]["lmp_email_format"]["plain"] == {"template_id": 8, "altbody": "hand text"}

    # …and back again uses the handmade template and its own text part.
    lm2 = _client(_camp(template_id=5, attribs=payload["attribs"]), [DESIGN_TPL, handmade])
    res = await ef.set_email_format(lm2, 6, "plain")
    assert res["template_id"] == 8
    assert lm2.update_campaign.await_args.args[1]["altbody"] == "hand text"
    lm2.create_template.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_twin_without_memory_switches_back_to_its_source():
    twin = {"id": 12, "name": "seq1 · plain", "type": "campaign", "body": ef.derive_plain_html(DESIGN, 5)}
    default = {"id": 1, "name": "Default", "type": "campaign", "body": "<table>{{ template \"content\" . }}</table>",
               "is_default": True}
    lm = _client(_camp(template_id=12), [default, DESIGN_TPL, twin])
    res = await ef.set_email_format(lm, 6, "html")
    assert res["template_id"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("camp, status", [
    (_camp(status="running"), 409),
    (_camp(status="finished"), 409),
    (_camp(content_type="visual"), 409),
    (_camp(content_type="plain"), 409),
])
async def test_blocked_campaigns_are_refused(camp, status):
    lm = _client(camp, [DESIGN_TPL])
    with pytest.raises(ef.FormatError) as exc:
        await ef.set_email_format(lm, 6, "plain")
    assert exc.value.status == status
    lm.update_campaign.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_format_is_a_no_op():
    lm = _client(_camp(), [DESIGN_TPL])
    res = await ef.set_email_format(lm, 6, "html")
    assert res["changed"] is False
    lm.update_campaign.assert_not_awaited()


@pytest.mark.asyncio
async def test_compile_failure_rolls_back():
    lm = _client(_camp(altbody="orig", attribs={"keep": True}), [DESIGN_TPL])
    lm.preview_campaign = AsyncMock(side_effect=RuntimeError("template: content:1: unexpected EOF"))
    with pytest.raises(ef.FormatError) as exc:
        await ef.set_email_format(lm, 6, "plain")
    assert exc.value.status == 422
    rollback = lm.update_campaign.await_args.args[1]
    assert rollback["template_id"] == 5
    assert rollback["altbody"] == "orig"
    assert rollback["attribs"] == {"keep": True}


@pytest.mark.asyncio
async def test_listing_reports_format_and_why_a_campaign_is_locked():
    camps = [_camp(id=1, status="running"), _camp(id=2, status="finished"), _camp(id=3, template_id=12)]
    twin = {"id": 12, "name": "seq1 · plain", "type": "campaign", "body": ef.derive_plain_html(DESIGN, 5)}
    lm = _client(camps[0], [DESIGN_TPL, twin])
    lm.get_campaigns = AsyncMock(return_value={"data": {"results": camps}})
    rows = {r["id"]: r for r in await ef.list_email_formats(lm)}
    assert set(rows) == {1, 3}  # finished campaigns are not listed
    assert rows[1]["can_switch"] is False and "Pause" in rows[1]["reason"]
    assert rows[3]["format"] == "plain" and rows[3]["template_name"] == "seq1 · plain"


# ── API ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app)


def test_switch_endpoint_validates_and_maps_errors(client):
    assert client.put("/api/deliverability/email-format/6", json={"format": "pdf"}).status_code == 400
    assert client.put("/api/deliverability/email-format/0", json={"format": "plain"}).status_code == 422
    with patch("app.routers.deliverability.set_email_format",
               AsyncMock(side_effect=ef.FormatError(409, "Pause the campaign to switch."))):
        res = client.put("/api/deliverability/email-format/6", json={"format": "plain"})
    assert res.status_code == 409 and "Pause" in res.json()["detail"]


def test_campaign_edit_keeps_attribs(client):
    with patch("app.routers.campaigns.listmonk.get_campaign", new_callable=AsyncMock) as get, \
         patch("app.routers.campaigns.listmonk.update_campaign", new_callable=AsyncMock) as upd:
        get.return_value = {"data": {"attribs": {"lmp_email_format": {"html": {"template_id": 5}}}}}
        upd.return_value = {"data": {}}
        res = client.put("/api/campaigns/6", json={"name": "x", "subject": "y"})
    assert res.status_code == 200
    sent = upd.await_args.args[1]
    assert sent["attribs"] == {"lmp_email_format": {"html": {"template_id": 5}}}
    assert sent["name"] == "x"
