"""Regression tests for the campaign test-send endpoint.

ListMonk's POST /api/campaigns/{id}/test binds a full campaign struct and
validates it (name/subject length, list IDs, messenger) before resolving the
``subscribers`` recipients as existing ListMonk subscribers. The dashboard
used to forward only ``{"emails": [...]}``, which ListMonk answered with
"Invalid length for name." for every request.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from tests.conftest import CSRFTestClient

from app.main import app

FAKE_CAMPAIGN = {
    "id": 7,
    "name": "1st Campaign",
    "subject": "Hello",
    "lists": [{"id": 3, "name": "Prospects"}],
    "from_email": "hi@x.com",
    "messenger": "email",
    "type": "regular",
    "tags": ["q3"],
    "template_id": 1,
    "content_type": "richtext",
    "body": "<p>Hi</p>",
    "altbody": None,
    "headers": [],
}


@pytest.fixture
def captured(monkeypatch):
    """Stub the ListMonk client and capture the final test-send POST."""
    from app.routers import campaigns as C

    captured = {}

    async def fake_get_campaign(campaign_id):
        return {"data": dict(FAKE_CAMPAIGN)}

    async def fake_request(method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kwargs.get("json", {})
        return {"data": True}

    monkeypatch.setattr(C.listmonk, "get_campaign", fake_get_campaign)
    monkeypatch.setattr(C.listmonk, "_request", fake_request)
    return captured


def test_test_send_posts_full_campaign_payload(captured):
    c = CSRFTestClient(app, raise_server_exceptions=False)
    with patch("app.main.verify_session", return_value=True):
        resp = c.post("/api/campaigns/7/test", json={"emails": ["  You@Example.COM "]})

    assert resp.status_code == 200
    assert captured["path"] == "/api/campaigns/7/test"
    payload = captured["json"]
    assert payload["subscribers"] == ["you@example.com"]  # trimmed + lowered
    assert payload["name"] == "1st Campaign"  # full campaign payload, or ListMonk rejects it
    assert payload["subject"] == "Hello"
    assert payload["lists"] == [3]  # int IDs, not the list objects GET returns
    assert payload["body"] == "<p>Hi</p>"
    assert "send_at" not in payload


def test_test_send_rejects_missing_recipient(captured):
    c = CSRFTestClient(app, raise_server_exceptions=False)
    with patch("app.main.verify_session", return_value=True):
        resp = c.post("/api/campaigns/7/test", json={"emails": []})
    assert resp.status_code == 400


def test_test_send_accepts_listmonk_style_subscribers_key(captured):
    c = CSRFTestClient(app, raise_server_exceptions=False)
    with patch("app.main.verify_session", return_value=True):
        resp = c.post("/api/campaigns/7/test", json={"subscribers": ["a@x.com"]})
    assert resp.status_code == 200
    assert captured["json"]["subscribers"] == ["a@x.com"]
