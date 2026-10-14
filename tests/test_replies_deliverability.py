import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from app.main import app
from app.services.imap_reply_tracker import classify_sentiment
from app.services.deliverability_service import get_deliverability_checklist, apply_deliverability_preset


def test_classify_sentiment():
    # Out of office
    assert classify_sentiment("Out of office", "I am out of the office until Monday") == "auto_reply"
    assert classify_sentiment("Re: Quick question", "Automatic reply: away from my desk") == "auto_reply"

    # Negative
    assert classify_sentiment("Re: Hello", "Please remove me from your list") == "negative"
    assert classify_sentiment("Re: Offer", "Not interested, unsubscribe") == "negative"

    # Positive
    assert classify_sentiment("Re: Video production", "Sounds good, let's talk tomorrow") == "positive"
    assert classify_sentiment("Re: Quick question", "Can you send over pricing and schedule a demo?") == "positive"
    assert classify_sentiment("Re: Followup", "Yes please, happy to connect") == "positive"

    # Question
    assert classify_sentiment("Re: Inquiry", "How does this work with our existing stack?") == "question"


def test_classify_sentiment_polite_decline_is_negative():
    # A courteous "no" carries no blunt refusal phrase; it must not fall
    # through to the question bucket (regression: a real prospect reply
    # saying exactly this was badged "Question").
    decline = (
        "Hello Kawser! Thank you very much for reaching out and sharing your "
        "proposal with us.\n\n"
        "At this time, we are not exploring this type of collaboration, but we "
        "have saved your contact information and will certainly get in touch if "
        "the situation changes. We truly appreciate your professionalism and "
        "wish you great success with your projects.\n\n"
        "Wishing you a productive and wonderful day ahead!"
    )
    assert classify_sentiment("Re: Question About Rollerads?", decline) == "negative"
    assert classify_sentiment(
        "Re: Offer", "We're not currently looking for a new vendor, but thanks anyway"
    ) == "negative"
    assert classify_sentiment(
        "Re: Offer", "Not a good fit for us right now — best of luck!"
    ) == "negative"
    assert classify_sentiment(
        "Re: Offer", "We have no need for this service at the moment."
    ) == "negative"
    assert classify_sentiment(
        "Re: Offer", "I'll pass on this for now, thanks for thinking of us."
    ) == "negative"
    assert classify_sentiment(
        "Re: Offer", "We'll keep you in mind should our needs change."
    ) == "negative"


@pytest.mark.asyncio
async def test_deliverability_checklist_evaluation():
    mock_listmonk = MagicMock()
    mock_listmonk.get_settings = AsyncMock(return_value={
        "data": {
            "privacy.disable_tracking": True,
            "privacy.individual_tracking": False,
            "privacy.unsubscribe_header": False,
            "app.batch_size": 100,
            "app.concurrency": 1,
            "app.enable_public_archive": False,
            # A fully wired instance also needs the bounce loop + safe uploads,
            # otherwise the score is not 100 (the audit used to ignore both).
            "bounce.enabled": True,
            "bounce.mailboxes": [{"enabled": True, "host": "mail.example.com"}],
            "bounce.actions": {"hard": {"count": 1, "action": "blocklist"}},
            "upload.extensions": ["jpg", "png"],
            "app.message_rate": 1,
            "app.message_sliding_window": True,
            "app.message_sliding_window_duration": "2m",
            "app.message_sliding_window_rate": 1,
        }
    })

    result = await get_deliverability_checklist(mock_listmonk)
    assert result["score"] == 100
    assert result["overall_status"] == "optimal"
    assert len(result["checks"]) == 10
    # Informational context (send cadence) is reported but never scored.
    assert [c["id"] for c in result["checks"] if c["status"] == "info"] == ["send_cadence"]


@pytest.mark.asyncio
async def test_deliverability_flags_missing_bounce_loop_and_upload_wildcard():
    mock_listmonk = MagicMock()
    mock_listmonk.get_settings = AsyncMock(return_value={
        "data": {
            "privacy.disable_tracking": True,
            "privacy.individual_tracking": False,
            "privacy.unsubscribe_header": False,
            "app.batch_size": 100,
            "app.concurrency": 1,
            "app.enable_public_archive": False,
            # Placeholder mailbox, no provider, wildcard uploads.
            "bounce.enabled": True,
            "bounce.mailboxes": [{"enabled": False, "host": "pop.yoursite.com"}],
            "bounce.actions": {"hard": {"count": 1, "action": "blocklist"}},
            "upload.extensions": ["jpg", "*"],
        }
    })

    result = await get_deliverability_checklist(mock_listmonk)
    by_id = {c["id"]: c for c in result["checks"]}
    assert by_id["bounce_feedback"]["status"] == "fail"
    assert by_id["upload_extensions"]["status"] == "fail"
    assert by_id["hard_bounce_action"]["status"] == "pass"
    # 9 scored checks (send_cadence is informational), 7 passing.
    assert result["score"] == 77
    assert result["overall_status"] == "good"


@pytest.mark.asyncio
async def test_deliverability_reports_send_cadence_per_day():
    mock_listmonk = MagicMock()
    mock_listmonk.get_settings = AsyncMock(return_value={
        "data": {
            "privacy.disable_tracking": True,
            "privacy.individual_tracking": False,
            "privacy.unsubscribe_header": False,
            "app.batch_size": 100,
            "app.concurrency": 1,
            "app.enable_public_archive": False,
            "bounce.enabled": True,
            "bounce.mailboxes": [{"enabled": True}],
            "bounce.actions": {"hard": {"action": "blocklist"}},
            "upload.extensions": ["png"],
            "app.message_sliding_window": True,
            "app.message_sliding_window_duration": "2m",
            "app.message_sliding_window_rate": 1,
        }
    })

    result = await get_deliverability_checklist(mock_listmonk)
    # 1 message per 2 minutes = 720/day, surfaced so slow drains are visible.
    assert result["raw_settings"]["send_cadence_per_day"] == 720
    cadence = next(c for c in result["checks"] if c["id"] == "send_cadence")
    assert "720" in cadence["current_value"]


@pytest.mark.asyncio
async def test_apply_deliverability_preset():
    mock_listmonk = MagicMock()
    mock_listmonk.apply_settings = AsyncMock(return_value={"data": True})

    res = await apply_deliverability_preset(mock_listmonk, "cold_email")
    assert res["status"] == "ok"
    assert res["applied"]["privacy.disable_tracking"] is True
    assert res["applied"]["privacy.unsubscribe_header"] is False
    assert res["applied"]["app.batch_size"] == 100
    # The preset must merge, not send a partial payload: ListMonk rejects a
    # settings PUT without an SMTP block.
    assert mock_listmonk.apply_settings.await_count == 1
    assert mock_listmonk.update_settings.call_count == 0

    res_nl = await apply_deliverability_preset(mock_listmonk, "newsletter")
    assert res_nl["status"] == "ok"
    assert res_nl["applied"]["privacy.disable_tracking"] is False
    assert res_nl["applied"]["privacy.unsubscribe_header"] is True


# ── Settings round-trip: masked secrets must never be written back ──────────

def test_sanitize_settings_blanks_masked_secrets():
    from app.services.listmonk_client import sanitize_settings_for_update

    raw = {
        "smtp": [{
            "uuid": "4a7163a7-4ded-4349-92d4-973d2820de7b",
            "host": "mail.example.com",
            "password": "\u2022" * 16,
            "from_addresses": ["@example.com"],
        }],
        "bounce.mailboxes": [{"uuid": "b1", "password": "\u2022" * 8, "host": "pop.example.com"}],
        "bounce.postmark": {"username": "postmark", "password": "\u2022" * 5},
        "upload.s3.aws_secret_access_key": "\u2022" * 20,
        "security.oidc": {"client_secret": "\u2022" * 10, "provider_name": "Google"},
        "app.batch_size": 100,
        "privacy.disable_tracking": True,
    }

    clean = sanitize_settings_for_update(raw)

    assert clean["smtp"][0]["password"] == ""
    assert clean["smtp"][0]["uuid"] == raw["smtp"][0]["uuid"]      # UUID kept
    assert clean["smtp"][0]["host"] == "mail.example.com"
    assert clean["smtp"][0]["from_addresses"] == ["@example.com"]
    assert clean["bounce.mailboxes"][0]["password"] == ""
    assert clean["bounce.postmark"]["password"] == ""
    assert clean["upload.s3.aws_secret_access_key"] == ""
    assert clean["security.oidc"]["client_secret"] == ""
    assert clean["security.oidc"]["provider_name"] == "Google"
    assert clean["app.batch_size"] == 100
    assert clean["privacy.disable_tracking"] is True


@pytest.mark.asyncio
async def test_apply_settings_merges_patch(monkeypatch):
    from app.services.listmonk_client import ListMonkClient

    client = ListMonkClient()
    current = {
        "smtp": [{"uuid": "u1", "host": "mail.example.com", "password": "\u2022" * 12}],
        "app.batch_size": 1000,
        "privacy.disable_tracking": False,
    }
    sent: dict = {}

    async def fake_get_settings():
        return {"data": current}

    async def fake_request(method, path, **kwargs):
        sent["method"] = method
        sent["path"] = path
        sent["json"] = kwargs.get("json")
        return {"data": True}

    monkeypatch.setattr(client, "get_settings", fake_get_settings)
    monkeypatch.setattr(client, "_request", fake_request)

    await client.apply_settings({"app.batch_size": 100})

    assert sent["method"] == "PUT" and sent["path"] == "/api/settings"
    assert sent["json"]["app.batch_size"] == 100           # patch applied
    assert sent["json"]["privacy.disable_tracking"] is False  # untouched key preserved
    assert sent["json"]["smtp"][0]["password"] == ""       # secret not clobbered
    assert sent["json"]["smtp"][0]["host"] == "mail.example.com"


@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield TestClient(app)


def test_replies_api_endpoints(client):
    # Test status
    res = client.get("/api/replies/status")
    assert res.status_code == 200
    assert "configured" in res.json()

    # Test stats
    res = client.get("/api/replies/stats")
    assert res.status_code == 200
    assert "total_replies" in res.json()
    assert "reply_rate" in res.json()

    # Test get replies
    res = client.get("/api/replies")
    assert res.status_code == 200
    assert "results" in res.json()


def test_deliverability_api_endpoints(client):
    with patch("app.routers.deliverability.listmonk.get_settings", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {
            "data": {
                "privacy.disable_tracking": True,
                "privacy.individual_tracking": False,
                "privacy.unsubscribe_header": False,
                "app.batch_size": 100,
                "app.concurrency": 1,
                "app.enable_public_archive": False,
                "bounce.enabled": True,
                "bounce.mailboxes": [{"enabled": True}],
                "bounce.actions": {"hard": {"action": "blocklist"}},
                "upload.extensions": ["png"],
            }
        }
        res = client.get("/api/deliverability/checklist")
        assert res.status_code == 200
        # A fully wired instance scores 100; the audit no longer ignores the
        # bounce mailbox or the upload allow-list.
        assert res.json()["score"] == 100
