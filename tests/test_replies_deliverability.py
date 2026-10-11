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
        }
    })

    result = await get_deliverability_checklist(mock_listmonk)
    assert result["score"] == 100
    assert result["overall_status"] == "optimal"
    assert len(result["checks"]) == 6


@pytest.mark.asyncio
async def test_apply_deliverability_preset():
    mock_listmonk = MagicMock()
    mock_listmonk.update_settings = AsyncMock(return_value={"data": True})

    res = await apply_deliverability_preset(mock_listmonk, "cold_email")
    assert res["status"] == "ok"
    assert res["applied"]["privacy.disable_tracking"] is True
    assert res["applied"]["privacy.unsubscribe_header"] is False
    assert res["applied"]["app.batch_size"] == 100

    res_nl = await apply_deliverability_preset(mock_listmonk, "newsletter")
    assert res_nl["status"] == "ok"
    assert res_nl["applied"]["privacy.disable_tracking"] is False
    assert res_nl["applied"]["privacy.unsubscribe_header"] is True


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
            }
        }
        res = client.get("/api/deliverability/checklist")
        assert res.status_code == 200
        assert res.json()["score"] >= 80
