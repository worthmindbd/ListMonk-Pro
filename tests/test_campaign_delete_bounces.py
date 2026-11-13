"""Deleting a campaign together with its bounce records (opt-in).

ListMonk keeps a deleted campaign's bounces with ``campaign: null``, where
they can no longer be found by campaign — so the bounces must be deleted
first, and the campaign kept when that fails.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.main import app
from app.services.bounce_list import delete_campaign_bounces
from app.services.listmonk_client import ListMonkClient
from tests.conftest import CSRFTestClient


@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app, raise_server_exceptions=False)


def _bounce(bid, cid, btype="hard"):
    return {"id": bid, "type": btype, "email": f"u{bid}@example.com",
            "campaign": {"id": cid, "name": f"C{cid}"} if cid else None}


def _fake_lm(bounces_by_campaign, fail_ids=()):
    """A ListMonk double whose bounce listing honours campaign_id and paging."""
    lm = AsyncMock()
    lm.deleted_bounces = []

    async def get_bounces(page=1, per_page=50, campaign_id=None, source="", bounce_type=""):
        rows = bounces_by_campaign.get(campaign_id, [])
        start = (page - 1) * per_page
        return {"data": {"results": rows[start:start + per_page], "total": len(rows)}}

    async def delete_bounce(bid):
        if bid in fail_ids:
            raise RuntimeError("upstream 500")
        lm.deleted_bounces.append(bid)
        return {"data": True}

    lm.get_bounces = get_bounces
    lm.delete_bounce = delete_bounce
    lm.iter_pages = lambda fn, **kw: ListMonkClient.iter_pages(None, fn, **kw)
    lm.delete_campaign = AsyncMock(return_value={"data": True})
    lm.delete_campaigns = AsyncMock(return_value={"data": True})
    return lm


@pytest.mark.anyio
async def test_helper_deletes_every_page_including_soft_bounces():
    rows = [_bounce(i, 7, "soft" if i % 2 else "hard") for i in range(1, 251)]
    lm = _fake_lm({7: rows})
    res = await delete_campaign_bounces(lm, 7)
    assert res == {"deleted": 250, "errors": 0, "failed": []}
    assert sorted(lm.deleted_bounces) == list(range(1, 251))


@pytest.mark.anyio
async def test_helper_never_deletes_another_campaigns_bounce():
    # A ListMonk that ignored the campaign_id filter must not turn this into
    # a delete-all: every record's campaign is re-checked.
    lm = _fake_lm({7: [_bounce(1, 7), _bounce(2, 8), _bounce(3, None)]})
    res = await delete_campaign_bounces(lm, 7)
    assert res["deleted"] == 1
    assert lm.deleted_bounces == [1]


def test_single_delete_without_option_leaves_bounces(client):
    lm = _fake_lm({5: [_bounce(1, 5)]})
    with patch("app.routers.campaigns.listmonk", lm):
        resp = client.delete("/api/campaigns/5")
    assert resp.status_code == 200
    assert lm.deleted_bounces == []
    lm.delete_campaign.assert_awaited_once_with(5)


def test_single_delete_with_option_deletes_bounces_first(client):
    lm = _fake_lm({5: [_bounce(1, 5), _bounce(2, 5, "soft")]})
    with patch("app.routers.campaigns.listmonk", lm), \
         patch("app.routers.campaigns.schedule_hard_bounce_update") as refresh:
        resp = client.delete("/api/campaigns/5?delete_bounces=true")
    assert resp.status_code == 200
    assert resp.json()["bounces_deleted"] == 2
    assert sorted(lm.deleted_bounces) == [1, 2]
    lm.delete_campaign.assert_awaited_once_with(5)
    refresh.assert_called_once()


def test_single_delete_keeps_campaign_when_a_bounce_fails(client):
    lm = _fake_lm({5: [_bounce(1, 5), _bounce(2, 5)]}, fail_ids={2})
    with patch("app.routers.campaigns.listmonk", lm), \
         patch("app.routers.campaigns.schedule_hard_bounce_update"):
        resp = client.delete("/api/campaigns/5?delete_bounces=true")
    assert resp.status_code == 502
    assert "kept" in resp.json()["detail"]
    lm.delete_campaign.assert_not_awaited()


def test_single_delete_keeps_campaign_when_listmonk_is_down(client):
    lm = _fake_lm({})

    async def down(**kw):
        raise httpx.ConnectError("refused")

    lm.get_bounces = down
    with patch("app.routers.campaigns.listmonk", lm), \
         patch("app.routers.campaigns.schedule_hard_bounce_update"):
        resp = client.delete("/api/campaigns/5?delete_bounces=true")
    assert resp.status_code == 502
    lm.delete_campaign.assert_not_awaited()


def test_bulk_delete_with_option_deletes_only_clean_campaigns(client):
    lm = _fake_lm({3: [_bounce(1, 3)], 4: [_bounce(2, 4), _bounce(3, 4)], 6: []},
                  fail_ids={3})
    with patch("app.routers.campaigns.listmonk", lm), \
         patch("app.routers.campaigns.schedule_hard_bounce_update"):
        resp = client.delete("/api/campaigns?ids=3&ids=4&ids=6&delete_bounces=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == 2
    assert body["kept"] == [4]
    assert body["errors"] == 1 and body["failed"][0]["id"] == 4
    assert body["bounces_deleted"] == 2
    assert lm.delete_campaigns.await_args.args[0] == [3, 6]


def test_bulk_delete_with_option_502_when_nothing_could_be_cleaned(client):
    lm = _fake_lm({3: [_bounce(1, 3)]}, fail_ids={1})
    with patch("app.routers.campaigns.listmonk", lm), \
         patch("app.routers.campaigns.schedule_hard_bounce_update"):
        resp = client.delete("/api/campaigns?ids=3&delete_bounces=true")
    assert resp.status_code == 502
    lm.delete_campaigns.assert_not_awaited()


def test_bulk_delete_without_option_is_unchanged(client):
    lm = _fake_lm({3: [_bounce(1, 3)]})
    with patch("app.routers.campaigns.listmonk", lm):
        resp = client.delete("/api/campaigns?ids=3&ids=4")
    assert resp.status_code == 200
    assert lm.deleted_bounces == []
    assert lm.delete_campaigns.await_args.args[0] == [3, 4]
