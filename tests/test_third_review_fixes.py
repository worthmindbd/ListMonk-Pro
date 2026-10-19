"""Regressions for the third full-codebase review.

Pins every defect the review fixed so none of them can silently return:
XFF-safe rate limiting lives in test_review_fixes; this file covers the
silent-truncation caps, the safe search builders, reclassification
attribution, honest scan/checklist failures, cancellation-safe opener
fetches, schedule validation, page-wise bounce counting, shared bulk-id
validation and the converter's worker-thread offload.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from tests.conftest import CSRFTestClient

from app.main import app


@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app, raise_server_exceptions=False)


# ── B2: exports/sweeps walk the whole table, no silent cap ──────────────

@pytest.mark.asyncio
async def test_bounce_sweep_walks_past_the_old_page_cap():
    """iter_filtered_bounces used to stop at MAX_LM_PAGES (300) and drop the
    rest of the table from exports and history-wide deletes."""
    from app.services import bounce_list

    TOTAL = 305

    class FakeClient:
        async def get_bounces(self, page, per_page, campaign_id=None,
                              source="", bounce_type=""):
            results = [{"id": page, "type": "hard", "email": f"e{page}@x.com"}
                       ] if page <= TOTAL else []
            return {"data": {"results": results, "total": TOTAL}}

    got = [b async for b in bounce_list.iter_filtered_bounces(
        FakeClient(), bounce_type="hard", fetch_size=1,
    )]
    assert len(got) == TOTAL


def test_subscriber_export_streams_past_the_old_page_cap(client):
    """export-all advertised 'every subscriber' but capped at 100 pages."""
    from app.routers import subscribers as sub_router

    TOTAL = 10_050  # 101 pages of 100 — one past the old EXPORT_MAX_PAGES

    async def fake_get_subscribers(page=1, per_page=50, query="", list_id=None):
        remaining = TOTAL - (page - 1) * per_page
        rows = [
            {"id": (page - 1) * per_page + i, "email": f"u{(page - 1) * per_page + i}@x.com",
             "name": "", "status": "enabled", "created_at": "", "updated_at": ""}
            for i in range(min(per_page, remaining))
        ]
        return {"data": {"results": rows, "total": TOTAL}}

    with patch.object(sub_router.listmonk, "get_subscribers",
                      side_effect=fake_get_subscribers):
        resp = client.get("/api/subscribers/export-all")
    assert resp.status_code == 200
    lines = [l for l in resp.text.splitlines() if l.strip()]
    assert len(lines) == TOTAL + 1  # header + every row


# ── F1: free-text search never reaches ListMonk as raw SQL ──────────────

def test_campaigns_search_becomes_escaped_ilike(client):
    from app.routers import campaigns as camp_router

    captured = {}

    async def fake_get_campaigns(page, per_page, query, status,
                                 order_by, order):
        captured["query"] = query
        return {"data": {"results": [], "total": 0}}

    with patch.object(camp_router.listmonk, "get_campaigns",
                      side_effect=fake_get_campaigns):
        resp = client.get("/api/campaigns?search=o'brien%20%25x")
    assert resp.status_code == 200
    q = captured["query"]
    assert "ILIKE" in q and "campaigns.name" in q and "campaigns.subject" in q
    assert "o''brien" in q             # quote doubled
    assert "\\%x" in q                 # % is escaped, not stripped
    assert "%o'brien" not in q         # never the raw unescaped term


def test_lists_search_becomes_escaped_ilike(client):
    from app.routers import lists as lists_router

    captured = {}

    async def fake_get_lists(page, per_page, query, status,
                             order_by, order, minimal):
        captured["query"] = query
        return {"data": {"results": [], "total": 0}}

    with patch.object(lists_router.listmonk, "get_lists",
                      side_effect=fake_get_lists):
        resp = client.get("/api/lists?search=black%20friday")
    assert resp.status_code == 200
    assert captured["query"] == "(lists.name ILIKE '%black friday%')"


# ── B3: reclassified auto-replies lose campaign attribution ─────────────

@pytest.mark.asyncio
async def test_reclassify_to_auto_reply_clears_campaign(tmp_path, monkeypatch):
    import app.services.reply_log as rl
    from app.services.imap_reply_tracker import reclassify_stored_sentiments

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "reply_log.json")
    monkeypatch.setattr(rl, "PROCESSED_FILE", tmp_path / "reply_processed.json")
    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    rl.save_log([{
        "id": "rep_9", "message_id": "<m9@x.com>", "from_email": "ceo@x.com",
        "subject": "Re: quick question", "full_body":
            "I am out of the office until Monday with no mail access.",
        "body_preview": "I am out of the office", "sentiment": "positive",
        "status": "lead", "status_source": "auto",
        "campaign_id": 5, "campaign_name": "Spring Outreach",
    }])

    changed = await reclassify_stored_sentiments()
    assert changed == 1
    rec = rl.load_log()[0]
    assert rec["sentiment"] == "auto_reply"
    assert rec["campaign_id"] is None
    assert rec["campaign_name"] is None


# ── B4: a failed link scan must not answer 200 "Scan complete" ──────────

def test_unsubscribes_scan_502_when_link_scan_fails(client):
    from app.routers import unsubscribes as unsub_router

    async def fake_imap(*a, **kw):
        return {"processed": 0, "errors": 0, "message": "Scan complete"}

    async def fake_link(*a, **kw):
        return {"scanned_lists": 0, "new_found": 0, "processed": 0,
                "errors": 1, "message": "Failed to fetch lists: boom"}

    with patch.object(unsub_router, "scan_and_unsubscribe",
                      side_effect=fake_imap), \
         patch.object(unsub_router, "scan_link_unsubscribes",
                      side_effect=fake_link):
        resp = client.post("/api/unsubscribes/scan")
    assert resp.status_code == 502


# ── B6: cancelling one waiter must not kill the shared fetch ────────────

@pytest.mark.asyncio
async def test_opener_cache_waiter_cancellation_is_isolated():
    from app.services import opener_cache

    started = asyncio.Event()

    class FakeClient:
        def get_subscribers(self, **kw):
            return None

        async def paginate_all(self, fn, per_page=500, **kw):
            started.set()
            await asyncio.sleep(0.05)
            return [{"email": "opener@x.com"}]

    client = FakeClient()
    waiter_a = asyncio.create_task(opener_cache.get_opener_emails(client, 7))
    await started.wait()
    waiter_b = asyncio.create_task(opener_cache.get_opener_emails(client, 7))
    await asyncio.sleep(0)  # let B attach to the in-flight task
    waiter_a.cancel()
    try:
        await waiter_a
    except asyncio.CancelledError:
        pass
    # B must still resolve from the shared fetch, not die with A.
    emails = await asyncio.wait_for(waiter_b, timeout=2)
    assert emails == {"opener@x.com"}


# ── B7: hand-edited schedule bounds fall back to defaults ───────────────

def test_schedule_invalid_window_bounds_fall_back(tmp_path, monkeypatch):
    from app.services import campaign_scheduler as sched

    bad = tmp_path / "schedule.json"
    bad.write_text(json.dumps({
        "enabled": True, "timezone": "US/Eastern",
        "start_hour": 99, "start_minute": "8", "end_hour": -3,
        "end_minute": 75, "days": ["mon"],
    }), encoding="utf-8")
    monkeypatch.setattr(sched, "SCHEDULE_FILE", bad)

    loaded = sched.load_schedule()
    d = sched.DEFAULT_SCHEDULE
    assert (loaded["start_hour"], loaded["start_minute"],
            loaded["end_hour"], loaded["end_minute"]) == (
        d["start_hour"], d["start_minute"], d["end_hour"], d["end_minute"])
    assert loaded["days"] == ["mon"]  # valid keys survive


# ── B8: deliverability checklist reports upstream outage as 502 ─────────

def test_deliverability_checklist_502_on_upstream_error(client):
    from app.routers import deliverability as deliv_router

    async def fake_checklist(listmonk):
        return {"score": 0, "overall_status": "error",
                "error": "connection refused", "checks": [],
                "groups": [], "raw_settings": {}}

    with patch.object(deliv_router, "get_deliverability_checklist",
                      side_effect=fake_checklist):
        resp = client.get("/api/deliverability/checklist")
    assert resp.status_code == 502


# ── B9: hard-bounce refresh counts page-wise (no paginate_all) ──────────

@pytest.mark.asyncio
async def test_hard_bounce_cache_counts_via_iter_pages(monkeypatch):
    from app.services import hard_bounce_cache as hbc

    async def identity_filter(client, bounces):
        return bounces

    monkeypatch.setattr(hbc, "filter_bounces_excluding_openers", identity_filter)

    class FakeLM:
        _client = object()

        def get_bounces(self, **kw):
            return None

        async def iter_pages(self, fn, per_page=500, **kw):
            for cid in (3, 3, 4):
                yield {"id": cid, "type": "hard",
                       "campaign": {"id": cid, "name": f"c{cid}"}}

        async def paginate_all(self, *a, **kw):  # pragma: no cover
            raise AssertionError("refresh must not materialise the table")

    monkeypatch.setattr(hbc, "listmonk", FakeLM())
    await hbc.update_hard_bounce_counts()
    assert hbc.get_all_hard_bounce_counts() == {3: 2, 4: 1}


# ── B10: bounce selection ids go through the shared validator ───────────

def test_bounce_delete_dedupes_repeated_ids(client):
    from app.routers import bounces as bounces_router

    calls = []

    async def fake_delete(bounce_id):
        calls.append(bounce_id)
        return {"data": None}

    with patch.object(bounces_router.listmonk, "delete_bounce",
                      side_effect=fake_delete):
        resp = client.delete("/api/bounces?ids=1&ids=1&ids=2")
    assert resp.status_code == 200
    assert sorted(calls) == [1, 2]
    assert resp.json()["deleted"] == 2
    assert resp.json()["errors"] == 0


def test_bounce_delete_rejects_non_positive_ids(client):
    with patch("app.routers.bounces.schedule_hard_bounce_update"):
        resp = client.delete("/api/bounces?ids=-5")
    assert resp.status_code == 400


# ── B5: converter CPU work runs off the event loop ──────────────────────

def test_converter_detect_columns_still_works_off_loop(client, tmp_path):
    """Behavioral guard for the asyncio.to_thread refactor."""
    csv = b"email,name,company\na@x.com,Ann,Acme\nb@x.com,Bob,Bcc\n"
    resp = client.post(
        "/api/converter/detect-columns",
        files={"file": ("leads.csv", csv, "text/csv")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "email" in json.dumps(body).lower()
