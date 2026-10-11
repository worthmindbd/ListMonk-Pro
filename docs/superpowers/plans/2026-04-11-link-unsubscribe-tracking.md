# Link Unsubscribe Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Poll ListMonk's API hourly for subscribers who clicked the built-in unsubscribe link, apply the same extended actions as IMAP-detected unsubscribes (remove from all lists + optional blocklist), and show them in the unified unsubscribe log with a source badge.

**Architecture:** A new `app/services/link_unsubscribe.py` service iterates over all ListMonk lists, paginates through unsubscribed members, diffs against the existing log to find new entries, processes them identically to IMAP unsubscribes, and records them with `source: "link"`. The hourly background loop in `app/main.py` calls both scanners. The frontend shows source badges and a stat breakdown.

**Tech Stack:** Python 3.12, FastAPI, httpx (async HTTP), vanilla JS frontend, JSON file storage (`unsubscribe_log.json`), pytest + pytest-asyncio for tests.

---

## File Map

| Action | File | Purpose |
|--------|------|---------|
| Create | `app/services/link_unsubscribe.py` | Link-based unsubscribe scanner |
| Create | `tests/__init__.py` | Test package marker |
| Create | `tests/test_link_unsubscribe.py` | Tests for the new scanner |
| Modify | `app/services/listmonk_client.py` | Add `get_subscribers_by_list_status()` |
| Modify | `app/services/imap_unsubscribe.py` | Add `source: "email"` to new records; add `link_count`/`email_count` to `get_stats()` |
| Modify | `app/routers/unsubscribes.py` | Update `/scan` to run both scanners; update `/stats` to return source counts |
| Modify | `app/main.py` | Import and call `scan_link_unsubscribes` in `imap_scan_loop` |
| Modify | `static/js/unsubscribes.js` | Source badges in table, stats breakdown, combined scan toast |

---

## Task 1: Test Infrastructure Setup

**Files:**
- Create: `tests/__init__.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Add pytest dependencies to requirements.txt**

Open `requirements.txt` and append these two lines:
```
pytest==9.0.3
pytest-asyncio==1.3.0
```

- [ ] **Step 2: Install the new dependencies**

Run from the project root (activate venv first if not already active):
```bash
source venv/bin/activate
pip install pytest==9.0.3 pytest-asyncio==1.3.0
```
Expected: Both packages install without errors.

- [ ] **Step 3: Create the tests package**

Create `tests/__init__.py` as an empty file:
```python
```

- [ ] **Step 4: Verify pytest runs**

```bash
pytest tests/ -v
```
Expected: `no tests ran` or `0 passed` — no errors, just nothing to run yet.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt tests/__init__.py
git commit -m "chore: add pytest test infrastructure"
```

---

## Task 2: Add `get_subscribers_by_list_status()` to ListMonkClient

**Files:**
- Modify: `app/services/listmonk_client.py` (after line ~48, in the Subscribers section)
- Create: `tests/test_link_unsubscribe.py` (first test only)

- [ ] **Step 1: Write the failing test**

Create `tests/test_link_unsubscribe.py`:
```python
import pytest
from unittest.mock import AsyncMock, MagicMock

# ── Helpers ──────────────────────────────────────────────────────────────────

def make_client(get_response=None):
    """Return a ListMonkClient whose _request is mocked."""
    from app.services.listmonk_client import ListMonkClient
    client = ListMonkClient.__new__(ListMonkClient)
    client._client = MagicMock()
    client._request = AsyncMock(return_value=get_response or {})
    return client


# ── get_subscribers_by_list_status ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_subscribers_by_list_status_passes_correct_params():
    fake_response = {"data": {"results": [], "total": 0}}
    client = make_client(fake_response)

    result = await client.get_subscribers_by_list_status(
        list_id=5,
        subscription_status="unsubscribed",
        page=1,
        per_page=100,
    )

    client._request.assert_called_once_with(
        "GET", "/api/subscribers",
        params={
            "list_id": 5,
            "subscription_status": "unsubscribed",
            "page": 1,
            "per_page": 100,
        }
    )
    assert result == fake_response
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_link_unsubscribe.py::test_get_subscribers_by_list_status_passes_correct_params -v
```
Expected: FAIL — `AttributeError: 'ListMonkClient' object has no attribute 'get_subscribers_by_list_status'`

- [ ] **Step 3: Add the method to ListMonkClient**

In `app/services/listmonk_client.py`, add this method inside the `ListMonkClient` class, after the `get_subscribers` method (around line 48):

```python
async def get_subscribers_by_list_status(
    self,
    list_id: int,
    subscription_status: str,
    page: int = 1,
    per_page: int = 100,
) -> dict:
    """Get subscribers for a list filtered by their subscription status."""
    params = {
        "list_id": list_id,
        "subscription_status": subscription_status,
        "page": page,
        "per_page": per_page,
    }
    return await self._request("GET", "/api/subscribers", params=params)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_link_unsubscribe.py::test_get_subscribers_by_list_status_passes_correct_params -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/listmonk_client.py tests/test_link_unsubscribe.py
git commit -m "feat: add get_subscribers_by_list_status to ListMonkClient"
```

---

## Task 3: Create `app/services/link_unsubscribe.py`

**Files:**
- Create: `app/services/link_unsubscribe.py`
- Modify: `tests/test_link_unsubscribe.py` (add more tests)

### 3a — Write tests for the core scanner

- [ ] **Step 1: Add tests to `tests/test_link_unsubscribe.py`**

Append these tests to the existing file:

```python
# ── scan_link_unsubscribes ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scan_skips_already_logged_emails(tmp_path, monkeypatch):
    """Emails already in the log must not be re-processed."""
    import json
    from app.services import link_unsubscribe as svc

    log_file = tmp_path / "unsubscribe_log.json"
    log_file.write_text(json.dumps([
        {"email": "already@example.com", "source": "link"}
    ]))
    monkeypatch.setattr(svc, "LOG_FILE", log_file)
    monkeypatch.setattr(svc, "SETTINGS_FILE", tmp_path / "settings.json")

    client = make_client()
    # Lists returns one list
    client._request = AsyncMock(side_effect=[
        {"data": {"results": [{"id": 1, "name": "Newsletter"}], "total": 1}},
        # Subscribers for list 1: returns the already-logged email
        {"data": {"results": [{"id": 99, "email": "already@example.com", "name": "Test", "lists": [{"id": 1}]}], "total": 1}},
    ])

    result = await svc.scan_link_unsubscribes(client)

    assert result["new_found"] == 0
    assert result["processed"] == 0


@pytest.mark.asyncio
async def test_scan_processes_new_link_unsubscribe(tmp_path, monkeypatch):
    """A new link-unsubscribed email should be processed and logged."""
    import json
    from app.services import link_unsubscribe as svc

    log_file = tmp_path / "unsubscribe_log.json"
    log_file.write_text("[]")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"blocklist_enabled": false}')
    monkeypatch.setattr(svc, "LOG_FILE", log_file)
    monkeypatch.setattr(svc, "SETTINGS_FILE", settings_file)

    client = make_client()
    client._request = AsyncMock(side_effect=[
        # GET /api/lists
        {"data": {"results": [{"id": 1, "name": "Newsletter"}], "total": 1}},
        # GET /api/subscribers (list 1, page 1) — 1 result, less than per_page → done
        {"data": {"results": [{"id": 55, "email": "new@example.com", "name": "New User", "lists": [{"id": 1}]}], "total": 1}},
        # GET /api/campaigns (for campaign matching) — no campaigns
        {"data": {"results": [], "total": 0}},
        # PUT /api/subscribers/lists (unsubscribe from all)
        {"data": {}},
    ])

    result = await svc.scan_link_unsubscribes(client)

    assert result["new_found"] == 1
    assert result["processed"] == 1
    assert result["errors"] == 0

    log = json.loads(log_file.read_text())
    assert len(log) == 1
    assert log[0]["email"] == "new@example.com"
    assert log[0]["source"] == "link"
    assert log[0]["keyword"] is None


@pytest.mark.asyncio
async def test_scan_blocklists_when_enabled(tmp_path, monkeypatch):
    """When blocklist_enabled=True, blocklist API is called."""
    import json
    from app.services import link_unsubscribe as svc

    log_file = tmp_path / "unsubscribe_log.json"
    log_file.write_text("[]")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"blocklist_enabled": true}')
    monkeypatch.setattr(svc, "LOG_FILE", log_file)
    monkeypatch.setattr(svc, "SETTINGS_FILE", settings_file)

    client = make_client()
    blocklist_calls = []

    async def mock_request(method, path, **kwargs):
        if method == "PUT" and "blocklist" in path:
            blocklist_calls.append(path)
            return {"data": {}}
        if method == "GET" and path == "/api/lists":
            return {"data": {"results": [{"id": 1, "name": "N"}], "total": 1}}
        if method == "GET" and "/api/subscribers" in path:
            return {"data": {"results": [{"id": 7, "email": "bl@example.com", "name": "BL", "lists": [{"id": 1}]}], "total": 1}}
        if method == "GET" and "/api/campaigns" in path:
            return {"data": {"results": [], "total": 0}}
        return {"data": {}}

    client._request = mock_request

    await svc.scan_link_unsubscribes(client)

    assert any("/blocklist" in c for c in blocklist_calls), "Blocklist API was not called"


@pytest.mark.asyncio
async def test_scan_pagination_fetches_second_page(tmp_path, monkeypatch):
    """When first page has exactly per_page results, a second page must be fetched."""
    import json
    from app.services import link_unsubscribe as svc

    log_file = tmp_path / "unsubscribe_log.json"
    log_file.write_text("[]")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"blocklist_enabled": false}')
    monkeypatch.setattr(svc, "LOG_FILE", log_file)
    monkeypatch.setattr(svc, "SETTINGS_FILE", settings_file)

    per_page = 2  # small for test
    page_calls = []

    async def mock_request(method, path, **kwargs):
        params = kwargs.get("params", {})
        if method == "GET" and path == "/api/lists":
            return {"data": {"results": [{"id": 1, "name": "N"}], "total": 1}}
        if method == "GET" and "/api/subscribers" in path:
            page = params.get("page", 1)
            page_calls.append(page)
            if page == 1:
                # Return exactly per_page results → trigger next page
                subs = [{"id": i, "email": f"u{i}@x.com", "name": "", "lists": [{"id": 1}]} for i in range(per_page)]
                return {"data": {"results": subs, "total": per_page + 1}}
            else:
                # Page 2: one result → stop
                return {"data": {"results": [{"id": 99, "email": "u99@x.com", "name": "", "lists": [{"id": 1}]}], "total": 1}}
        if method == "GET" and "/api/campaigns" in path:
            return {"data": {"results": [], "total": 0}}
        return {"data": {}}

    client = make_client()
    client._request = mock_request

    # Patch per_page constant used in the scanner
    monkeypatch.setattr(svc, "PER_PAGE", per_page)

    result = await svc.scan_link_unsubscribes(client)

    assert 2 in page_calls, "Second page was never fetched"
    assert result["new_found"] == per_page + 1


@pytest.mark.asyncio
async def test_scan_does_not_log_on_unsubscribe_api_failure(tmp_path, monkeypatch):
    """If the unsubscribe-from-all API call fails, subscriber must NOT be logged."""
    import json
    from app.services import link_unsubscribe as svc

    log_file = tmp_path / "unsubscribe_log.json"
    log_file.write_text("[]")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"blocklist_enabled": false}')
    monkeypatch.setattr(svc, "LOG_FILE", log_file)
    monkeypatch.setattr(svc, "SETTINGS_FILE", settings_file)

    async def mock_request(method, path, **kwargs):
        if method == "GET" and path == "/api/lists":
            return {"data": {"results": [{"id": 1, "name": "N"}], "total": 1}}
        if method == "GET" and "/api/subscribers" in path:
            return {"data": {"results": [{"id": 5, "email": "fail@example.com", "name": "", "lists": [{"id": 1}]}], "total": 1}}
        if method == "GET" and "/api/campaigns" in path:
            return {"data": {"results": [], "total": 0}}
        if method == "PUT" and "/api/subscribers/lists" in path:
            raise Exception("API error")
        return {"data": {}}

    client = make_client()
    client._request = mock_request

    result = await svc.scan_link_unsubscribes(client)

    assert result["errors"] == 1
    log = json.loads(log_file.read_text())
    assert len(log) == 0, "Failed subscriber must not be logged"
```

- [ ] **Step 2: Run tests to verify they all fail**

```bash
pytest tests/test_link_unsubscribe.py -v
```
Expected: All new tests FAIL with `ModuleNotFoundError: No module named 'app.services.link_unsubscribe'`

### 3b — Implement the scanner

- [ ] **Step 3: Create `app/services/link_unsubscribe.py`**

```python
"""
Link Unsubscribe Scanner: Polls ListMonk API for subscribers who used the
direct unsubscribe link. Applies the same actions as IMAP unsubscribes:
removes from all lists and optionally blocklists. Records in the shared
unsubscribe_log.json with source="link".
"""

import logging
from datetime import datetime
from pathlib import Path

from app.services.listmonk_client import ListMonkClient
from app.services.imap_unsubscribe import (
    load_log, save_log, load_settings, LOG_FILE, SETTINGS_FILE,
)

logger = logging.getLogger("link_unsubscribe")

PER_PAGE = 100  # Patchable in tests


async def _match_campaign_for_list(client: ListMonkClient, list_id: int) -> dict:
    """
    Return the most recent campaign (any status) that targets list_id.
    Returns {campaign_id, campaign_name, campaign_key} or all-None dict.
    """
    try:
        result = await client.get_campaigns(
            page=1, per_page=50, order_by="created_at", order="DESC"
        )
        campaigns = result.get("data", {}).get("results", [])
    except Exception as e:
        logger.warning(f"Could not fetch campaigns for list {list_id}: {e}")
        return {"campaign_id": None, "campaign_name": "Unknown", "campaign_key": _current_campaign_key()}

    now = datetime.utcnow()
    for camp in campaigns:
        created = camp.get("created_at", "")
        if not created:
            continue
        try:
            camp_date = datetime.fromisoformat(created[:10])
        except (ValueError, TypeError):
            continue
        if camp_date <= now:
            return {
                "campaign_id": camp.get("id"),
                "campaign_name": camp.get("name", ""),
                "campaign_key": f"{camp_date.year}-{camp_date.month:02d}",
            }

    return {"campaign_id": None, "campaign_name": "Unknown", "campaign_key": _current_campaign_key()}


def _current_campaign_key() -> str:
    now = datetime.utcnow()
    return f"{now.year}-{now.month:02d}"


async def scan_link_unsubscribes(client: ListMonkClient) -> dict:
    """
    Scan all ListMonk lists for link-unsubscribed subscribers and process them.
    Returns summary: {scanned_lists, new_found, processed, errors}.
    """
    scanned_lists = 0
    new_found = 0
    processed = 0
    errors = 0
    new_records = []

    # Load existing log for deduplication
    existing_log = load_log()
    processed_emails = {r["email"] for r in existing_log}

    scan_settings = load_settings()
    blocklist_enabled = scan_settings.get("blocklist_enabled", False)

    # Fetch all lists
    try:
        lists_result = await client.get_lists(page=1, per_page=200, minimal=True)
        all_lists = lists_result.get("data", {}).get("results", [])
    except Exception as e:
        logger.error(f"Failed to fetch lists: {e}")
        return {"scanned_lists": 0, "new_found": 0, "processed": 0, "errors": 1,
                "message": f"Failed to fetch lists: {e}"}

    for lst in all_lists:
        list_id = lst.get("id")
        list_name = lst.get("name", "")
        if not list_id:
            continue

        scanned_lists += 1
        page = 1

        # Paginate through all unsubscribed subscribers for this list
        while True:
            try:
                result = await client.get_subscribers_by_list_status(
                    list_id=list_id,
                    subscription_status="unsubscribed",
                    page=page,
                    per_page=PER_PAGE,
                )
                subscribers = result.get("data", {}).get("results", [])
            except Exception as e:
                logger.error(f"Failed to fetch unsubscribed subscribers for list {list_id}: {e}")
                errors += 1
                break

            for sub in subscribers:
                email = (sub.get("email") or "").lower()
                if not email or email in processed_emails:
                    continue

                new_found += 1
                sub_id = sub.get("id")
                sub_lists = [lst["id"] for lst in sub.get("lists", [])]

                # Match to a campaign
                campaign = await _match_campaign_for_list(client, list_id)

                # Unsubscribe from all lists — idempotent for already-unsubscribed list
                try:
                    if sub_lists:
                        await client.modify_list_memberships({
                            "ids": [sub_id],
                            "action": "unsubscribe",
                            "target_list_ids": sub_lists,
                            "status": "unsubscribed",
                        })
                except Exception as e:
                    logger.error(f"Failed to unsubscribe {email} from lists: {e}")
                    errors += 1
                    continue  # Do not log — partial failure

                # Optionally blocklist
                if blocklist_enabled:
                    try:
                        await client.blocklist_subscriber(sub_id)
                        logger.info(f"[LINK] Blocklisted: {email}")
                    except Exception as e:
                        logger.error(f"Failed to blocklist {email}: {e}")
                        # Partial success — still log the unsubscribe

                record = {
                    "email": email,
                    "name": sub.get("name", ""),
                    "source": "link",
                    "keyword": None,
                    "list_id": list_id,
                    "campaign_id": campaign["campaign_id"],
                    "campaign_name": campaign["campaign_name"],
                    "campaign_key": campaign["campaign_key"],
                    "subscriber_id": sub_id,
                    "lists_removed": sub_lists,
                    "timestamp": datetime.utcnow().isoformat(),
                }
                new_records.append(record)
                processed_emails.add(email)
                processed += 1
                action = "Unsubscribed + Blocklisted" if blocklist_enabled else "Unsubscribed"
                logger.info(f"[LINK] {action}: {email} (list: {list_name})")

            # Termination: stop when fewer results than per_page were returned
            if len(subscribers) < PER_PAGE:
                break
            page += 1

    # Persist new records
    if new_records:
        existing_log = load_log()
        existing_log.extend(new_records)
        save_log(existing_log)

    return {
        "scanned_lists": scanned_lists,
        "new_found": new_found,
        "processed": processed,
        "errors": errors,
        "message": f"Link scan complete: {processed} unsubscribed",
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_link_unsubscribe.py -v
```
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/link_unsubscribe.py tests/test_link_unsubscribe.py
git commit -m "feat: add link unsubscribe scanner service"
```

---

## Task 4: Update `imap_unsubscribe.py` — Add `source` field and stats breakdown

**Files:**
- Modify: `app/services/imap_unsubscribe.py`

Two changes: (a) stamp new IMAP records with `source: "email"`, (b) extend `get_stats()` to return `link_count` and `email_count`.

- [ ] **Step 1: Add `source: "email"` to new IMAP records**

In `app/services/imap_unsubscribe.py`, find the `record = {...}` dict that builds each new log entry (around line 449). Add `"source": "email"` to it:

```python
record = {
    "email": sender_email,
    "name": subscriber.get("name", ""),
    "source": "email",          # ← add this line
    "keyword": matched_keyword,
    "subject": subject,
    "message_id": msg_message_id,
    "campaign_key": f"{email_date.year}-{email_date.month:02d}",
    "campaign_id": campaign["campaign_id"],
    "campaign_name": campaign["campaign_name"],
    "subscriber_id": sub_id,
    "lists_removed": sub_lists,
    "timestamp": datetime.utcnow().isoformat(),
}
```

- [ ] **Step 2: Extend `get_stats()` to return source counts**

In `app/services/imap_unsubscribe.py`, find the `get_stats()` function (around line 501). Replace the return statement with:

```python
def get_stats() -> dict:
    """Return aggregate stats from the unsubscribe log."""
    records = load_log()
    total = len(records)

    today = datetime.utcnow().date().isoformat()
    today_count = sum(1 for r in records if r.get("timestamp", "").startswith(today))

    week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
    week_count = sum(1 for r in records if r.get("timestamp", "") >= week_ago)

    # Source breakdown — records without 'source' are treated as 'email'
    link_count = sum(1 for r in records if r.get("source") == "link")
    email_count = total - link_count

    return {
        "total": total,
        "today": today_count,
        "this_week": week_count,
        "link_count": link_count,
        "email_count": email_count,
    }
```

- [ ] **Step 3: Verify existing tests still pass**

```bash
pytest tests/ -v
```
Expected: All tests PASS (no regressions).

- [ ] **Step 4: Commit**

```bash
git add app/services/imap_unsubscribe.py
git commit -m "feat: add source field to IMAP records and source counts to stats"
```

---

## Task 5: Update Router — Unified Scan and Stats

**Files:**
- Modify: `app/routers/unsubscribes.py`

Two changes: (a) `POST /scan` runs both IMAP and link scanners, (b) `GET /stats` returns the new `link_count`/`email_count` fields (already handled by the updated `get_stats()` — just verify the endpoint passes them through).

- [ ] **Step 1: Update the import at the top of `app/routers/unsubscribes.py`**

The current imports are:
```python
from app.services.imap_unsubscribe import (
    load_log, save_log, get_stats, check_imap_status, scan_and_unsubscribe,
    load_settings, save_settings,
)
```

Add the link scanner import after it:
```python
from app.services.link_unsubscribe import scan_link_unsubscribes
```

- [ ] **Step 2: Replace the `trigger_scan` endpoint**

Find the `@router.post("/scan")` endpoint (around line 168) and replace it:

```python
@router.post("/scan")
async def trigger_scan():
    """Manually trigger both IMAP and link-based unsubscribe scans."""
    try:
        imap_result = await scan_and_unsubscribe(listmonk)
        link_result = await scan_link_unsubscribes(listmonk)
        total_processed = imap_result.get("processed", 0) + link_result.get("processed", 0)
        return {
            "imap": imap_result,
            "link": link_result,
            "message": f"Scan complete: {total_processed} unsubscribed",
        }
    except Exception as e:
        return {"error": str(e)}
```

- [ ] **Step 3: Verify the stats endpoint passes through new fields**

Check the `GET /stats` endpoint (around line 46). It calls `get_stats()` and returns its result directly — `link_count` and `email_count` will now be included automatically since `get_stats()` returns them. No change needed.

- [ ] **Step 4: Run all tests**

```bash
pytest tests/ -v
```
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/routers/unsubscribes.py
git commit -m "feat: unified scan endpoint runs both IMAP and link scanners"
```

---

## Task 6: Update `app/main.py` — Add Link Scan to Background Loop

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Add the import**

In `app/main.py`, find the existing import:
```python
from app.services.imap_unsubscribe import scan_and_unsubscribe
```

Replace with:
```python
from app.services.imap_unsubscribe import scan_and_unsubscribe
from app.services.link_unsubscribe import scan_link_unsubscribes
```

- [ ] **Step 2: Update `imap_scan_loop` to also run the link scanner**

Find the `imap_scan_loop` function (around line 71) and replace it:

```python
async def imap_scan_loop():
    """Scan IMAP inbox and ListMonk link unsubscribes every hour (no run on startup)."""
    while True:
        await asyncio.sleep(IMAP_SCAN_INTERVAL)
        try:
            imap_result = await scan_and_unsubscribe(listmonk)
            if imap_result.get("processed", 0) > 0:
                logger.info(f"IMAP scan: {imap_result['processed']} unsubscribe(s) processed")
        except Exception as e:
            logger.error(f"IMAP scan error: {e}")
        try:
            link_result = await scan_link_unsubscribes(listmonk)
            if link_result.get("processed", 0) > 0:
                logger.info(f"Link scan: {link_result['processed']} unsubscribe(s) processed")
        except Exception as e:
            logger.error(f"Link unsubscribe scan error: {e}")
```

Also update the startup log message in `lifespan()` (around line 90) from:
```python
logger.info("Background tasks started: auto-unblock (6h), campaign scheduler (60s), IMAP scan (1h)")
```
To:
```python
logger.info("Background tasks started: auto-unblock (6h), campaign scheduler (60s), IMAP scan (1h), link scan (1h)")
```

- [ ] **Step 3: Verify app starts without import errors**

```bash
python -c "from app.main import app; print('OK')"
```
Expected: prints `OK` with no errors.

- [ ] **Step 4: Commit**

```bash
git add app/main.py
git commit -m "feat: run link unsubscribe scan alongside IMAP scan every hour"
```

---

## Task 7: Frontend — Source Badges, Stats Breakdown, Scan Toast

**Files:**
- Modify: `static/js/unsubscribes.js`

Three changes: (a) source badge helper + column rename, (b) stats breakdown row in monitor card, (c) updated scan toast message.

### 7a — Source badge helper and column rename

- [ ] **Step 1: Add `renderSourceBadge` helper**

In `static/js/unsubscribes.js`, find the `cleanCampaignName(name)` method (around line 32). Add the new helper **after** it:

```js
/** Render a source badge for a log record. */
renderSourceBadge(record) {
    const source = record.source || 'email';
    if (source === 'link') {
        return '<span class="badge badge-primary" title="Clicked unsubscribe link">link click</span>';
    }
    const kw = (record.keyword || 'email reply').replace(/</g, '&lt;');
    return `<span class="badge badge-warning" title="${kw}">email reply</span>`;
},
```

- [ ] **Step 2: Rename the table column header and use the new helper**

In `renderDetailView()`, find the `<thead>` row (around line 257):
```js
<th>#</th><th>Email</th><th>Name</th><th>Keyword</th><th>Date</th><th>Actions</th>
```
Change `Keyword` to `Type`:
```js
<th>#</th><th>Email</th><th>Name</th><th>Type</th><th>Date</th><th>Actions</th>
```

Then in the `records.forEach` loop (around line 270), find the table row template. Replace the `keyword` cell:

Find:
```js
const keyword = (r.keyword || '-').replace(/</g, '&lt;');
```
And in the `html +=` row template, change:
```js
<td><span class="badge badge-warning">${keyword}</span></td>
```
To:
```js
<td>${Unsubscribes.renderSourceBadge(r)}</td>
```

Also remove the now-unused `keyword` variable line (`const keyword = (r.keyword || '-').replace(/</g, '&lt;');`) that appears just before the `html +=` row template — it is no longer referenced.

### 7b — Stats breakdown in monitor card

- [ ] **Step 3: Add source breakdown to the monitor card**

In `renderListView()`, find the unsub-monitor-info section (around line 106). It has several `<div>` rows for scan interval, keywords, scan scope, and action. Add a new `<div>` **after** the action toggle div:

```js
<div>
    <span style="color:var(--text-muted)">Sources:</span>
    <strong>${App.formatNumber(stats.email_count || 0)} email repl${(stats.email_count || 0) !== 1 ? 'ies' : 'y'}</strong>
    <span style="color:var(--text-muted);margin:0 6px">·</span>
    <strong>${App.formatNumber(stats.link_count || 0)} link click${(stats.link_count || 0) !== 1 ? 's' : ''}</strong>
</div>
```

### 7c — Combined scan toast

- [ ] **Step 4: Update the scan result toast in `triggerScan()`**

In `triggerScan()` (around line 418), find:
```js
const msg = `Scanned ${result.scanned || 0} emails — ${result.processed || 0} unsubscribed`;
App.toast(msg, result.processed > 0 ? 'success' : 'info');
```

Replace with:
```js
const imapData = result.imap || result;  // supports both old and new format
const linkData = result.link || {};
const totalProcessed = (imapData.processed || 0) + (linkData.processed || 0);
const msg = `Scanned ${imapData.scanned || 0} emails + ${linkData.scanned_lists || 0} lists — ${totalProcessed} unsubscribed`;
App.toast(msg, totalProcessed > 0 ? 'success' : 'info');
```

- [ ] **Step 5: Manual smoke test**

Start the app and navigate to the Unsubscribes page. Verify:
1. The monitor card shows a "Sources: N email replies · N link clicks" line
2. The records table column is labeled "Type" (not "Keyword")
3. Click "Scan Now" — toast shows "Scanned X emails + Y lists — Z unsubscribed"
4. If there are existing records: verify old records (no `source` field) show `email reply` badge; link records show `link click` badge

- [ ] **Step 6: Commit**

```bash
git add static/js/unsubscribes.js
git commit -m "feat: add source badges, stats breakdown, and combined scan toast"
```

---

## Task 8: Final Verification

- [ ] **Step 1: Run all tests**

```bash
pytest tests/ -v
```
Expected: All tests PASS.

- [ ] **Step 2: Start the app and verify no startup errors**

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Expected: Starts cleanly. Log line reads: `Background tasks started: auto-unblock (6h), campaign scheduler (60s), IMAP scan (1h)`

- [ ] **Step 3: Trigger a manual scan via the API**

```bash
curl -s -X POST http://localhost:8000/api/unsubscribes/scan \
  -H "Cookie: <your-session-cookie>" | python -m json.tool
```
Expected: Response with both `imap` and `link` keys and a combined `message`.

- [ ] **Step 4: Final commit if needed, then done**

```bash
git status
# Commit any remaining uncommitted changes
```
