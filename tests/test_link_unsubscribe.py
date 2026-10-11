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
    monkeypatch.setattr(svc, "LOG_FILE", log_file, raising=False)
    monkeypatch.setattr(svc, "SETTINGS_FILE", tmp_path / "settings.json", raising=False)
    # Also patch the shared module that link_unsubscribe now imports from
    import app.services.unsubscribe_log as shared_log
    monkeypatch.setattr(shared_log, "LOG_FILE", log_file)
    monkeypatch.setattr(shared_log, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(shared_log, "PROCESSED_FILE", tmp_path / "processed.json")

    client = make_client()

    async def mock_request(method, path, **kwargs):
        if method == "GET" and path == "/api/lists":
            return {"data": {"results": [{"id": 1, "name": "Newsletter"}], "total": 1}}
        if method == "GET" and "/api/subscribers" in path:
            # Returns the already-logged email
            return {"data": {"results": [{"id": 99, "email": "already@example.com", "name": "Test", "lists": [{"id": 1}]}], "total": 1}}
        return {"data": {}}

    client._request = mock_request

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
    import app.services.unsubscribe_log as shared_log
    monkeypatch.setattr(shared_log, "LOG_FILE", log_file)
    monkeypatch.setattr(shared_log, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(shared_log, "PROCESSED_FILE", tmp_path / "processed.json")

    client = make_client()

    async def mock_request(method, path, **kwargs):
        if method == "GET" and path == "/api/lists":
            return {"data": {"results": [{"id": 1, "name": "Newsletter"}], "total": 1}}
        if method == "GET" and "/api/subscribers" in path:
            # 1 result, less than per_page → single page
            return {"data": {"results": [{"id": 55, "email": "new@example.com", "name": "New User", "lists": [{"id": 1}]}], "total": 1}}
        if method == "GET" and "/api/campaigns" in path:
            # 1 campaign targeting list 1
            return {"data": {"results": [{"id": 10, "name": "April Newsletter", "created_at": "2026-04-14", "lists": [{"id": 1}]}], "total": 1}}
        if method == "PUT" and "/api/subscribers/lists" in path:
            return {"data": {}}
        return {"data": {}}

    client._request = mock_request

    result = await svc.scan_link_unsubscribes(client)

    assert result["new_found"] == 1
    assert result["processed"] == 1
    assert result["errors"] == 0

    log = json.loads(log_file.read_text())
    assert len(log) == 1
    assert log[0]["email"] == "new@example.com"
    assert log[0]["source"] == "link"
    assert log[0]["keyword"] is None
    assert log[0]["campaign_id"] == 10
    assert log[0]["campaign_name"] == "April Newsletter"


@pytest.mark.asyncio
async def test_scan_blocklists_when_enabled(tmp_path, monkeypatch):
    """When blocklist_enabled=True, blocklist API is called."""
    import json
    from app.services import link_unsubscribe as svc

    log_file = tmp_path / "unsubscribe_log.json"
    log_file.write_text("[]")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"blocklist_enabled": true}')
    monkeypatch.setattr(svc, "LOG_FILE", log_file, raising=False)
    monkeypatch.setattr(svc, "SETTINGS_FILE", settings_file, raising=False)
    import app.services.unsubscribe_log as shared_log
    monkeypatch.setattr(shared_log, "LOG_FILE", log_file)
    monkeypatch.setattr(shared_log, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(shared_log, "PROCESSED_FILE", tmp_path / "processed.json")

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
    monkeypatch.setattr(svc, "LOG_FILE", log_file, raising=False)
    monkeypatch.setattr(svc, "SETTINGS_FILE", settings_file, raising=False)
    import app.services.unsubscribe_log as shared_log
    monkeypatch.setattr(shared_log, "LOG_FILE", log_file)
    monkeypatch.setattr(shared_log, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(shared_log, "PROCESSED_FILE", tmp_path / "processed.json")

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
async def test_scan_attributes_to_newest_campaign_across_lists(tmp_path, monkeypatch):
    """When a subscriber is unsubscribed from multiple lists (e.g. list A and list B),
    they must be attributed to the most recent campaign that targets ANY of those
    lists — not to whichever list happens to be scanned first."""
    import json
    from app.services import link_unsubscribe as svc

    log_file = tmp_path / "unsubscribe_log.json"
    log_file.write_text("[]")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"blocklist_enabled": false}')
    monkeypatch.setattr(svc, "LOG_FILE", log_file, raising=False)
    monkeypatch.setattr(svc, "SETTINGS_FILE", settings_file, raising=False)
    import app.services.unsubscribe_log as shared_log
    monkeypatch.setattr(shared_log, "LOG_FILE", log_file)
    monkeypatch.setattr(shared_log, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(shared_log, "PROCESSED_FILE", tmp_path / "processed.json")

    # Same subscriber (id=100) appears as unsubscribed in BOTH list 1 and list 2.
    # Campaign 10 (older) targets list 1; campaign 20 (newer) targets list 2.
    # The subscriber should be attributed to campaign 20, regardless of iteration order.
    subscriber = {
        "id": 100,
        "email": "multi@example.com",
        "name": "Multi Lister",
        "lists": [{"id": 1}, {"id": 2}],
    }

    async def mock_request(method, path, **kwargs):
        params = kwargs.get("params", {})
        if method == "GET" and path == "/api/lists":
            return {"data": {"results": [
                {"id": 1, "name": "Old List"},
                {"id": 2, "name": "New List"},
            ], "total": 2}}
        if method == "GET" and "/api/subscribers" in path:
            return {"data": {"results": [subscriber], "total": 1}}
        if method == "GET" and "/api/campaigns" in path:
            return {"data": {"results": [
                {"id": 20, "name": "New Campaign", "created_at": "2026-04-10",
                 "lists": [{"id": 2}]},
                {"id": 10, "name": "Old Campaign", "created_at": "2026-03-01",
                 "lists": [{"id": 1}]},
            ], "total": 2}}
        if method == "PUT" and "/api/subscribers/lists" in path:
            return {"data": {}}
        return {"data": {}}

    client = make_client()
    client._request = mock_request

    result = await svc.scan_link_unsubscribes(client)

    assert result["processed"] == 1
    log = json.loads(log_file.read_text())
    assert len(log) == 1, "Subscriber should be logged exactly once, not once per list"
    assert log[0]["email"] == "multi@example.com"
    assert log[0]["campaign_id"] == 20, (
        f"Expected attribution to newest campaign (id=20), "
        f"got {log[0]['campaign_id']}"
    )


@pytest.mark.asyncio
async def test_scan_does_not_log_on_unsubscribe_api_failure(tmp_path, monkeypatch):
    """If the unsubscribe-from-all API call fails, subscriber must NOT be logged."""
    import json
    from app.services import link_unsubscribe as svc

    log_file = tmp_path / "unsubscribe_log.json"
    log_file.write_text("[]")
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"blocklist_enabled": false}')
    monkeypatch.setattr(svc, "LOG_FILE", log_file, raising=False)
    monkeypatch.setattr(svc, "SETTINGS_FILE", settings_file, raising=False)
    import app.services.unsubscribe_log as shared_log
    monkeypatch.setattr(shared_log, "LOG_FILE", log_file)
    monkeypatch.setattr(shared_log, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(shared_log, "PROCESSED_FILE", tmp_path / "processed.json")

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
