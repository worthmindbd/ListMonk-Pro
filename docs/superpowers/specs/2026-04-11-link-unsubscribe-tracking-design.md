# Link Unsubscribe Tracking — Design Spec

**Date:** 2026-04-11
**Status:** Approved

## Overview

Extend the existing unsubscribe system to detect and process subscribers who clicked the direct ListMonk unsubscribe link in campaign emails. These are currently invisible to the dashboard — only IMAP reply-based unsubscribes are tracked. Both sources will be unified into the same log and UI, with source badges distinguishing them.

## Background

The current system uses an IMAP scanner (`app/services/imap_unsubscribe.py`) that runs hourly, detects email replies containing keywords ("Remove me", "Unsubscribe me", "Exclude me"), and:
- Unsubscribes the sender from all ListMonk lists
- Optionally blocklists them (user-configurable toggle)
- Records the event in `unsubscribe_log.json`

When a subscriber clicks the built-in ListMonk unsubscribe link, ListMonk removes them from that specific list, but the dashboard does not capture it, no further action is taken (e.g., removal from other lists), and the event is invisible in the UI.

## Goals

- Detect link-based unsubscribes via periodic polling of the ListMonk API
- Apply the same extended actions: unsubscribe from ALL lists + optional blocklist
- Log them in the unified `unsubscribe_log.json` with `source: "link"`
- Display them in the same campaign-grouped UI with a source badge

## Non-Goals

- Real-time webhook integration (deferred — polling is sufficient)
- Separate UI section for link unsubscribes
- Migration of existing log records (old records without `source` default to `"email"`)

## Architecture

### New Service: `app/services/link_unsubscribe.py`

Runs on the same hourly schedule as the IMAP scan, called from the background scheduler in `app/main.py`.

**Flow:**
1. Fetch all lists from ListMonk (`GET /api/lists`, minimal mode)
2. For each list, paginate through subscribers with `subscription_status=unsubscribed` using `per_page=100`. Continue fetching pages until the number of results returned is less than `per_page` (termination condition).
3. Diff against existing `unsubscribe_log.json` — skip emails already recorded (any source)
4. For each new entry:
   a. Fetch subscriber's full record to get all current list memberships
   b. Unsubscribe from all lists via `PUT /api/subscribers/lists` with `action: "unsubscribe"`. ListMonk has already removed them from the triggering list; this call is idempotent for that list and also removes them from all others. No filtering of the triggering list is needed.
   c. If `blocklist_enabled`: blocklist the subscriber (`PUT /api/subscribers/{id}/blocklist`)
   d. Match to the most recent finished or sent campaign targeting that list (see Campaign Matching)
   e. Record in log with `source: "link"`
5. Return summary: `{ scanned_lists, new_found, processed, errors }`

### Campaign Matching

Query `GET /api/campaigns?list_id={id}&order_by=created_at&order=DESC&per_page=50` (no status filter, since ListMonk campaigns may be in `finished` or `sent` status) and pick the most recent campaign whose `created_at` date is on or before the scan timestamp. The `campaign_key` is derived as `YYYY-MM` from the campaign's `created_at` date (e.g., a campaign created on `2026-04-05` → `campaign_key: "2026-04"`). This matches the same derivation used in the IMAP scanner. If no campaign is found, `campaign_id` is `null`, `campaign_name` is `"Unknown"`, and `campaign_key` is the current year-month.

### Updated `ListMonkClient`

Add one method to `app/services/listmonk_client.py`:

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

### Unified Scan Trigger

`POST /api/unsubscribes/scan` (in `app/routers/unsubscribes.py`) runs both scans sequentially and returns a combined summary:

```json
{
  "imap": { "scanned": 42, "matched": 1, "processed": 1, "errors": 0 },
  "link": { "scanned_lists": 3, "new_found": 2, "processed": 2, "errors": 0 },
  "message": "Scan complete: 3 unsubscribed"
}
```

## Data Schema

### Log Record (link-sourced)

```json
{
  "email": "user@example.com",
  "name": "John Doe",
  "source": "link",
  "keyword": null,
  "list_id": 5,
  "campaign_id": 12,
  "campaign_name": "April Newsletter",
  "campaign_key": "2026-04",
  "subscriber_id": 42,
  "lists_removed": [5, 6, 7],
  "timestamp": "2026-04-11T10:00:00"
}
```

### Log Record (IMAP-sourced, updated going forward)

```json
{
  "email": "user@example.com",
  "name": "John Doe",
  "source": "email",
  "keyword": "remove me",
  "message_id": "<abc@mail.example.com>",
  "campaign_id": 12,
  "campaign_name": "April Newsletter",
  "campaign_key": "2026-04",
  "subscriber_id": 42,
  "lists_removed": [5, 6, 7],
  "timestamp": "2026-04-11T09:00:00"
}
```

**Backward compatibility:** Existing records without a `source` field are treated as `"email"` by the frontend (`record.source || 'email'`). No log migration required.

## Frontend Changes (`static/js/unsubscribes.js`)

### Records Table

Replace the `Keyword` column header with `Type`. Each cell renders a badge:
- `email reply` (amber) — IMAP-detected; keyword shown on hover via `title` attribute
- `link click` (blue) — direct unsubscribe link

```js
function renderSourceBadge(record) {
    const source = record.source || 'email';
    if (source === 'link') {
        return '<span class="badge badge-primary">link click</span>';
    }
    const kw = (record.keyword || 'email reply').replace(/</g, '&lt;');
    return `<span class="badge badge-warning" title="${kw}">email reply</span>`;
}
```

### Stats / Monitor Card

Add a source breakdown line to the IMAP monitor info section:

```
Sources:   N email replies    N link clicks
```

The `/api/unsubscribes/stats` endpoint will be extended to include `link_count` and `email_count` fields, computed server-side from `unsubscribe_log.json`. The frontend reads these new fields and renders the breakdown — no client-side record fetching or counting is required.

### Scan Toast

Updated to show combined result:
> "Scanned 42 emails + 3 lists — 3 unsubscribed"

## Deduplication Strategy

A subscriber is considered already processed if their email appears in `unsubscribe_log.json` with **any** source. This prevents:
- A subscriber who replied AND clicked the link from being double-processed
- Re-processing a link-unsubscribe on the next hourly scan

The dedup set is built from `{r["email"] for r in existing_log}` — same pattern as the current IMAP scanner.

## Error Handling

- If a list fetch fails: log warning, skip that list, continue
- If a subscriber lookup fails: log error, increment error count, continue
- If `blocklist` API call fails: log error, but still record the unsubscribe (partial success)
- If campaign matching fails: record with `campaign_id: null`, `campaign_name: "Unknown"`

## Testing Considerations

- Unit test `scan_link_unsubscribes()` with mocked `ListMonkClient`
- Verify deduplication: running scan twice should not double-process
- Verify `blocklist_enabled=False` skips blocklist call
- Verify campaign matching falls back gracefully when no campaign found
- Verify pagination termination: a list with exactly 100 unsubscribers triggers a second page fetch; a list with 99 stops after one page
- Verify partial failure: if `PUT /api/subscribers/lists` fails, the subscriber is NOT logged (no partial record written); error count is incremented
- Frontend: verify `renderSourceBadge` handles missing `source` field (old records)
