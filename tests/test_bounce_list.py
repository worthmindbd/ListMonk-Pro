"""Tests for bounce_list paginated fetching and type filtering."""

import pytest
from unittest.mock import AsyncMock
from app.services.bounce_list import fetch_filtered_bounces_page, fetch_all_filtered_bounces


@pytest.mark.asyncio
async def test_fetch_filtered_bounces_page_does_not_break_on_empty_batch(monkeypatch):
    """Verify that when page 1 has 0 hard bounces (e.g. only soft bounces),
    the loop continues to page 2 where hard bounces exist."""
    import app.services.bounce_list as bl_mod
    monkeypatch.setattr(bl_mod, "LM_FETCH_SIZE", 2)

    client = AsyncMock()

    page_1_data = {
        "data": {
            "results": [
                {"id": 1, "email": "soft1@example.com", "type": "soft", "campaign": {"id": 10}},
                {"id": 2, "email": "soft2@example.com", "type": "soft", "campaign": {"id": 10}},
            ],
            "total": 4,
        }
    }
    page_2_data = {
        "data": {
            "results": [
                {"id": 3, "email": "hard1@example.com", "type": "hard", "campaign": {"id": 10}},
                {"id": 4, "email": "hard2@example.com", "type": "hard", "campaign": {"id": 10}},
            ],
            "total": 4,
        }
    }

    async def mock_get_bounces(page=1, per_page=100, campaign_id=None, source="", bounce_type=""):
        if page == 1:
            return page_1_data
        elif page == 2:
            return page_2_data
        return {"data": {"results": [], "total": 4}}

    client.get_bounces = AsyncMock(side_effect=mock_get_bounces)
    client.get_subscribers = AsyncMock(return_value={"data": {"total": 0, "results": []}})

    res = await fetch_filtered_bounces_page(client, page=1, per_page=10, bounce_type="hard")
    results = res["data"]["results"]
    assert len(results) == 2
    assert [b["id"] for b in results] == [3, 4]


@pytest.mark.asyncio
async def test_fetch_filtered_bounces_page_soft_type():
    client = AsyncMock()
    data = {
        "data": {
            "results": [
                {"id": 1, "email": "soft1@example.com", "type": "soft", "campaign": {"id": 10}},
                {"id": 2, "email": "hard1@example.com", "type": "hard", "campaign": {"id": 10}},
            ],
            "total": 2,
        }
    }
    client.get_bounces = AsyncMock(return_value=data)
    res = await fetch_filtered_bounces_page(client, page=1, per_page=10, bounce_type="soft")
    results = res["data"]["results"]
    assert len(results) == 1
    assert results[0]["id"] == 1


@pytest.mark.asyncio
async def test_fetch_filtered_bounces_page_all_types():
    client = AsyncMock()
    data = {
        "data": {
            "results": [
                {"id": 1, "email": "soft1@example.com", "type": "soft", "campaign": {"id": 10}},
                {"id": 2, "email": "hard1@example.com", "type": "hard", "campaign": {"id": 10}},
            ],
            "total": 2,
        }
    }
    client.get_bounces = AsyncMock(return_value=data)
    client.get_subscribers = AsyncMock(return_value={"data": {"total": 0, "results": []}})
    res = await fetch_filtered_bounces_page(client, page=1, per_page=10, bounce_type="")
    results = res["data"]["results"]
    assert len(results) == 2
