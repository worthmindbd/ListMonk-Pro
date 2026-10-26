import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from tests.conftest import CSRFTestClient
from app.main import app


@pytest.fixture
def client():
    # Bypass auth middleware for testing
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app)


def test_get_campaign_analytics_views(client):
    mock_data = {
        "data": [
            {"campaign_id": 1, "count": 12, "timestamp": "2026-08-01T00:00:00Z"},
            {"campaign_id": 1, "count": 18, "timestamp": "2026-08-02T00:00:00Z"},
        ]
    }
    with patch("app.routers.campaigns.listmonk.get_campaign_analytics", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_data
        response = client.get("/api/campaigns/analytics/views?campaign_id=1&from_date=2026-08-01&to_date=2026-08-02")
        assert response.status_code == 200
        assert response.json() == mock_data
        # ListMonk filters created_at <= to: the end date is sent as the end
        # of that day, otherwise the whole last day drops out of the chart.
        mock_get.assert_called_once_with("views", 1, "2026-08-01", "2026-08-02 23:59:59")


def test_export_campaign_analytics_csv(client):
    mock_data = {
        "data": [
            {"timestamp": "2026-08-01", "count": 12},
            {"timestamp": "2026-08-02", "count": 18},
        ]
    }
    with patch("app.routers.campaigns.listmonk.get_campaign_analytics", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_data
        response = client.get("/api/campaigns/analytics/views/export?campaign_id=1&from_date=2026-08-01&to_date=2026-08-02")
        assert response.status_code == 200
        assert "text/csv" in response.headers.get("content-type", "")
        content = response.text
        assert "timestamp,count" in content
        assert "2026-08-01,12" in content
