# ListMonk Dashboard — Agent & Developer Guide

A modern, dark-themed management dashboard and automation suite for self-hosted [ListMonk](https://listmonk.app/) instances. Built with FastAPI (Python 3.12+) and vanilla JavaScript.

---

## Project Structure

```
Listmonk-Dashboard/
├── app/
│   ├── __init__.py
│   ├── auth.py                    # Session auth, HMAC-SHA256 cookie signing & verification
│   ├── config.py                  # Environment config, .env loader, DATA_DIR resolver
│   ├── main.py                    # FastAPI application, auth middleware, background task lifespan
│   ├── routers/                   # API route handlers
│   │   ├── __init__.py
│   │   ├── bounces.py             # Bounce management, manual ingest, filtered exports, deletions
│   │   ├── campaigns.py           # Campaign CRUD, stats, analytics, viewer/clicker/bounce exports
│   │   ├── converter.py           # CSV column detection, conversion & direct ListMonk import
│   │   ├── lists.py               # List CRUD endpoints
│   │   ├── subscribers.py         # Subscriber CRUD, memberships, blocklisting, exports
│   │   ├── templates.py           # Template CRUD & default template selection
│   │   └── unsubscribes.py        # Unsubscribe log, campaign groupings, scan trigger, reset/undo
│   └── services/                  # Core business logic & integrations
│       ├── __init__.py
│       ├── auto_unblock.py        # Auto-unblock engaged contacts (clickers/openers) from blocklist
│       ├── bounce_filters.py      # Smart bounce filtering (excludes campaign openers as false positives)
│       ├── bounce_ingest.py       # RFC 3463 bounce DSN classifier & IMAP bounce mailbox scanner
│       ├── bounce_list.py         # Paginated bounce listing with opener exclusion & cache integration
│       ├── campaign_scheduler.py  # Timezone-aware send window scheduler (auto-pause/auto-resume)
│       ├── csv_converter.py       # Converts arbitrary CSV to ListMonk schema (JSON attributes)
│       ├── export_service.py      # CSV generators (sync list + async streamed)
│       ├── hard_bounce_cache.py   # In-memory hard bounce counts per campaign with background refresh
│       ├── imap_helpers.py        # Shared IMAP utilities (body extraction, date formatting, query escaping)
│       ├── imap_unsubscribe.py    # IMAP reply monitor for unsubscribe keywords with quote stripping
│       ├── link_unsubscribe.py    # ListMonk direct link unsubscribe scanner & campaign attributor
│       ├── listmonk_client.py     # Async HTTP client for ListMonk REST API (with paginate_all helper)
│       ├── opener_cache.py        # In-memory campaign opener email cache with TTL & inflight deduplication
│       ├── task_utils.py          # Strong references for fire-and-forget tasks & shutdown drain
│       └── unsubscribe_log.py     # Persistent JSON state management for unsubscribe log/settings/processed
├── static/
│   ├── css/
│   │   └── style.css              # Dark theme CSS with custom variables & responsive design
│   ├── js/
│   │   ├── analytics.js           # Campaign analytics charts, date filtering, link tracking
│   │   ├── api.js                 # Centralized fetch wrapper with 401 redirect & error handling
│   │   ├── app.js                 # SPA router, notifications, navigation, bounces view & shared UI
│   │   ├── campaigns.js           # Campaign management, status transitions, test email sending
│   │   ├── charts.js              # Dashboard overview charts & stat summary cards
│   │   ├── converter.js           # CSV converter UI, column mapping, live preview & import modal
│   │   ├── lists.js               # List management, modal forms, tag pills
│   │   ├── settings.js            # Settings UI: send window scheduler, auto-unblock, unsubscribe settings
│   │   ├── subscribers.js         # Subscriber table, search, edit modal, bulk blocklist/delete
│   │   └── unsubscribes.js        # Campaign-grouped unsubscribe dashboard, source badges, undo reset
│   ├── favicon.png
│   └── favicon.svg
├── templates/
│   ├── index.html                 # Main authenticated SPA shell
│   └── login.html                 # Login page with dark theme
├── tests/
│   ├── __init__.py
│   ├── test_analytics.py          # Analytics endpoint & export tests
│   ├── test_bounce_classify.py    # RFC 3463 bounce classifier unit tests
│   ├── test_bounce_filters.py     # Opener exclusion & false positive filter tests
│   ├── test_link_unsubscribe.py   # Link unsubscribe scanner & dedup tests
│   ├── test_bounce_list.py        # Bounce pagination + type filtering
│   ├── test_campaign_scheduler.py # Send-window logic
│   ├── test_csv_converter.py      # Encoding/delimiter/column detection
│   ├── test_review_fixes.py       # Regression tests for the full-codebase review
│   ├── conftest.py                # Resets module-level caches between tests
│   └── test_regressions.py        # Route order, IMAP seen-marking, prune, timestamp regressions
├── .github/
│   └── workflows/
│       └── deploy.yml             # SSH automated deploy for multi-instance VPS
├── docs/
│   └── superpowers/               # Design specs and execution plans
├── .dockerignore
├── .env.example                   # Template for environment configuration
├── .gitignore
├── DESIGN.md                      # UI design system and token specification
├── Dockerfile                     # Production non-root container definition
├── docker-compose.yml             # Primary instance Docker compose
├── docker-compose.instance2.yml   # Second instance override compose (port 8001, data-2)
├── pytest.ini                     # Pytest configuration
├── requirements.txt               # Production Python dependencies
├── requirements-dev.txt           # Development and testing dependencies
├── start.sh                       # Local development startup script
└── README.md                      # Public documentation
```

---

## Technology Stack

- **Backend**: Python 3.12+, FastAPI 0.141+, Uvicorn, httpx (async HTTP), Jinja2, Python-dotenv
- **Frontend**: Vanilla JavaScript (ES6+), Chart.js (vendored in `static/js/vendor/`), Modern Dark CSS (no build step)
- **State & Storage**: Writable JSON state files in `DATA_DIR` (`schedule.json`, `unsubscribe_log.json`, `unsubscribe_settings.json`, `unsubscribe_processed.json`), in-memory caches (`opener_cache`, `hard_bounce_cache`)
- **Testing**: Pytest 9.1+, pytest-asyncio, AnyIO
- **Deployment**: Docker, Docker Compose, Nginx, Certbot

---

## Architecture & Subsystems

### 1. ListMonk Async API Client (`app/services/listmonk_client.py`)
- Async `httpx.AsyncClient` singleton (`listmonk`) with connection pooling, started/closed in the FastAPI `lifespan`.
- Implements full ListMonk REST API: subscribers, lists, campaigns, templates, bounces, imports, and analytics.
- Provides `paginate_all()` utility to automatically traverse paginated ListMonk endpoints with configurable page size.

### 2. Authentication & Middleware (`app/auth.py`, `app/main.py`)
- HMAC-SHA256 signed session cookies (`SESSION_SECRET`). If `SESSION_SECRET` is omitted, auto-generates a secure 32-byte secret and persists it to `DATA_DIR/session_secret.key` (chmod 600, git- and docker-ignored).
- 7-day session expiration.
- `AuthMiddleware` intercepts all requests:
  - Whitelisted paths: `/auth/login`, `/auth/logout`, `/favicon.ico`, `/static/*`.
  - `Strict-Transport-Security` is added only when the request arrived over TLS
    (directly or via `X-Forwarded-Proto`), so plain-HTTP dev is not poisoned.
  - Unauthenticated `/api/*` requests receive `401 Unauthorized`.
  - Unauthenticated HTML page requests redirect (`302`) to `/auth/login`.
- Login is rate-limited per client IP (10 failed attempts / 15 minutes → `429`).
  The IP comes from `X-Forwarded-For` only when the immediate peer is loopback/private,
  or when `TRUST_PROXY=true` — otherwise a spoofed header would let an attacker
  reset the limiter on every attempt. Credential comparison encodes to UTF-8 first
  so non-ASCII input is a normal `401` rather than a `500`.
- `SecurityHeadersMiddleware` sets `X-Content-Type-Options`, `Referrer-Policy`, and `X-Frame-Options` on every response.

### 3. Bounce Ingestion & Smart Filtering
- **Bounce Ingestion** (`app/services/bounce_ingest.py`):
  - Connects to bounce IMAP mailbox (`BOUNCE_IMAP_*`), reads unseen DSN messages.
  - Classifies into **hard** vs **soft** bounces using RFC 3463 enhanced status codes (e.g. `5.1.1`, `5.1.2`, `5.2.1`) and failure patterns.
  - Attributes bounces to campaigns via message headers (`X-Listmonk-Campaign`, `List-ID`) or list membership and dates.
  - Skips recipients who opened the campaign (cannot be genuine hard bounces).
  - Creates matching bounce records in ListMonk via API.
- **Opener Cache** (`app/services/opener_cache.py`):
  - In-memory cache of subscribers who opened a campaign (`campaign_views`), with a 10-minute TTL and in-flight request deduplication.
- **Hard Bounce Cache** (`app/services/hard_bounce_cache.py`):
  - In-memory cache of true hard bounce counts per campaign, refreshed on startup and every 5 minutes in background. Overrides ListMonk's raw bounce count in campaign lists once the first refresh completes.
- **Fast Filtered Listing** (`app/services/bounce_list.py`):
  - Fast-path pagination and CSV exports with opener exclusion. On a cold opener
    cache, batches larger than `COLD_CACHE_EMAIL_THRESHOLD` (20) fetch the
    campaign's full opener set rather than one query per bounce — that also warms
    `opener_cache`, so later pages are in-memory lookups. Small batches still use
    the per-email path under a `CHECK_CONCURRENCY=25` semaphore.
  - Tolerates ListMonk's `"campaign": null` (subscriber-level bounces); never uses
    chained `.get("campaign", {}).get(...)`, which breaks on an explicit null.

### 4. Dual-Source Unsubscribe Engine
- **IMAP Unsubscribe Monitor** (`app/services/imap_unsubscribe.py`):
  - Scans primary inbox (`IMAP_*`) every hour for **unseen** reply keywords (`"remove me"`, `"unsubscribe me"`, `"exclude me"`); blocking IMAP calls run via `asyncio.to_thread`.
  - Quote stripping (`_extract_reply_only`): ignores quoted history/template footers to eliminate false positives.
  - List-aware campaign matching: attributes replies to the latest campaign that targeted the subscriber's actual lists.
  - Actions: unenrolls subscriber from all lists, conditionally blocklists (if enabled in settings), logs to `unsubscribe_log.json`.
  - Processed messages are marked `\Seen` after their records are persisted; handled emails are also stored in `unsubscribe_processed.json`, so clearing/deleting log records does not cause re-processing.
- **Link Unsubscribe Scanner** (`app/services/link_unsubscribe.py`):
  - Polls ListMonk lists for subscribers with `unsubscribed` status.
  - Cascades unsubscription across all lists and applies optional blocklist.
  - Logs records with `source: "link"`; serialized with a module lock so manual and background scans cannot double-process.
- **Undo / Reset** (`POST /api/unsubscribes/reset`):
  - Re-subscribes users to their original removed lists, resets status to `enabled`, removes restored records from log, and unmarks their emails so future unsubscribes are detected again.

### 5. Automation Workers (FastAPI Lifespan)
All background tasks run as managed `asyncio.create_task` loops in `app/main.py`:
| Task Loop | Interval | Purpose |
|-----------|----------|---------|
| `auto_unblock_loop` | 6 hours | Finds blocklisted clickers/openers, re-enables them, deletes false bounce records |
| `scheduler_loop` | 60 seconds | Enforces daily send windows, auto-pauses/resumes campaigns |
| `imap_scan_loop` | 1 hour | Scans IMAP inbox for replies + polls ListMonk for link unsubscribes |
| `bounce_ingest_loop` | 1 hour | Scans bounce mailbox for unseen DSNs, classifies & records bounces |
| `start_cache_updater` | 5 minutes | Refreshes in-memory hard bounce counts per campaign |

### 6. CSV Converter (`app/services/csv_converter.py`)
- Detects column headers and sample data from uploaded CSVs.
- Maps custom columns to ListMonk's required structure: `email`, `name`, `attributes` (serialized as JSON).
- Supports direct import into ListMonk with list selection and subscription mode (`subscribe`, `unsubscribed`).

### 7. Persistent Runtime State (`DATA_DIR`)
- `app/config.py` provides `settings.data_path(filename)` to store mutable JSON state files (`schedule.json`, `unsubscribe_log.json`, `unsubscribe_settings.json`, `unsubscribe_processed.json`).
- In Docker, `DATA_DIR` defaults to `/data` (backed by a named volume).
- Automatically migrates legacy root JSON files into `DATA_DIR` on first run.

---

## API Endpoints Overview

| Category | Endpoint | Method | Description |
|----------|----------|--------|-------------|
| **Auth** | `/auth/login` | GET / POST | Login page / authenticate session |
| | `/auth/logout` | POST | End session & redirect (POST-only: a GET would be a forced-logout CSRF) |
| **Subscribers** | `/api/subscribers` | GET | Paginated subscribers with search/query/list filter |
| | `/api/subscribers/export-all` | GET | Stream all subscribers as CSV |
| | `/api/subscribers/import/status` | GET | Get subscriber import status |
| | `/api/subscribers/import/logs` | GET | Get subscriber import logs |
| | `/api/subscribers/{id}` | GET / PUT / DELETE | Get / update / delete subscriber |
| | `/api/subscribers/{id}/export` | GET | Export single subscriber JSON profile |
| | `/api/subscribers/{id}/bounces` | GET | Get bounces for subscriber |
| | `/api/subscribers/{id}/blocklist` | PUT | Blocklist single subscriber |
| | `/api/subscribers/blocklist` | PUT | Bulk blocklist subscribers by ID array |
| | `/api/subscribers/lists` | PUT | Bulk modify list memberships (add/remove/unsub) |
| | `/api/subscribers` | POST / DELETE | Create subscriber / bulk delete subscribers |
| **Lists** | `/api/lists` | GET / POST | List all mailing lists / create new list |
| | `/api/lists/{id}` | GET / PUT / DELETE | Get / update / delete list |
| **Campaigns** | `/api/campaigns` | GET / POST | List campaigns (with hard bounce cache) / create |
| | `/api/campaigns/running/stats` | GET | Real-time running campaign stats |
| | `/api/campaigns/summary` | GET | Aggregate totals across all campaigns (cached 60s) |
| | `/api/campaigns/export-all` | GET | Export all campaigns summary as CSV |
| | `/api/campaigns/analytics/{type}` | GET | Campaign analytics (views/clicks/bounces/links) |
| | `/api/campaigns/analytics/{type}/export` | GET | Export campaign analytics as CSV |
| | `/api/campaigns/{id}` | GET / PUT / DELETE | Get / update / delete campaign |
| | `/api/campaigns/{id}/preview` | GET | Preview campaign HTML rendering |
| | `/api/campaigns/{id}/status` | PUT | Change status (start, pause, cancel) |
| | `/api/campaigns/{id}/archive` | PUT | Archive campaign |
| | `/api/campaigns/{id}/test` | POST | Send test email |
| | `/api/campaigns/{id}/subscribers/{type}` | GET | View campaign viewers, clickers, or hard bounces |
| | `/api/campaigns/{id}/subscribers/{type}/export` | GET | Export viewers, clickers, or hard bounces as CSV |
| **Templates** | `/api/templates` | GET / POST | List templates / create template |
| | `/api/templates/{id}` | GET / PUT / DELETE | Get / update / delete template |
| | `/api/templates/{id}/default` | PUT | Set default template |
| **Bounces** | `/api/bounces` | GET / DELETE | List bounces (filtered) / bulk delete |
| | `/api/bounces/ingest` | POST | Trigger bounce mailbox IMAP ingestion |
| | `/api/bounces/export` | GET | Export filtered bounce records as CSV |
| | `/api/bounces/{id}` | DELETE | Delete single bounce record |
| **CSV Converter** | `/api/converter/detect-columns` | POST | Detect CSV headers & preview rows |
| | `/api/converter/convert` | POST | Convert CSV to ListMonk schema |
| | `/api/converter/convert-and-import` | POST | Convert and import directly into ListMonk |
| **Scheduler** | `/api/scheduler` | GET / PUT | Get / update send window settings |
| | `/api/scheduler/run` | POST | Manually trigger scheduler tick |
| **Auto-Unblock** | `/api/auto-unblock/status` | GET | Get count of blocklisted engaged contacts |
| | `/api/auto-unblock/run` | POST | Manually run auto-unblock process |
| **Unsubscribes** | `/api/unsubscribes` | GET | Paginated unsubscribe log |
| | `/api/unsubscribes/stats` | GET | Unsubscribe counts & source breakdown |
| | `/api/unsubscribes/settings` | GET / PUT | Get / update unsubscribe settings (blocklist toggle) |
| | `/api/unsubscribes/campaigns` | GET | Unsubscribes grouped by campaign |
| | `/api/unsubscribes/campaign/{id}` | GET / DELETE | Get / delete records for specific campaign |
| | `/api/unsubscribes/campaign/{id}/export` | GET | Export campaign unsubscribes as CSV |
| | `/api/unsubscribes/records` | DELETE | Delete specific records by email |
| | `/api/unsubscribes/export` | GET | Export all unsubscribes as CSV |
| | `/api/unsubscribes/scan` | POST | Manually trigger IMAP + link scan |
| | `/api/unsubscribes/imap-status` | GET | Check IMAP connection health |
| | `/api/unsubscribes/clear` | DELETE | Clear all unsubscribe log records |
| | `/api/unsubscribes/reset` | POST | Undo all unsubscribes & restore list memberships |

---

## Development & Testing Workflow

### Running Tests
Always run the full test suite before committing code:
```bash
./venv/bin/pytest tests/ -v
```

### Running the Dev Server
```bash
./start.sh
# or manually:
source venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Coding Rules for Agents
1. **Never make up file paths** — verify existing files before editing.
2. **Never break existing comments and docstrings** — preserve documentation integrity.
3. **Escaping ListMonk SQL queries** — use `safe_email_for_query` from `imap_helpers.py` when passing user input to ListMonk queries.
4. **Memory efficiency** — for CSV exports, prefer `aiter_dicts_to_csv` from `export_service.py` fed by `listmonk.iter_pages(...)` so rows stream one page at a time. `dict_list_to_csv` still buffers its input list, so only use it when the dataset is already materialised (e.g. after in-process filtering).
5. **Bound pagination inputs** — every endpoint taking `page`/`per_page` must declare `Query(..., ge=1, le=MAX)`. Bounce listing is the expensive one: serving page N scans every preceding record, so an unbounded `per_page` multiplies into tens of thousands of ListMonk queries.
6. **Never chain `.get(k, {}).get(...)`** on ListMonk response fields that can be explicitly null (notably `bounce["campaign"]`). Use the helpers in `bounce_filters.py` (`bounce_campaign_id` / `bounce_campaign_name`).
7. **Failed actions return a status code** — raise `HTTPException` rather than returning `200 {"error": ...}`, so monitoring and the frontend can distinguish success from failure. The one exception is `/api/auto-unblock/status`, which degrades to a `200` with an `error` field because the Settings page loads it via `Promise.all`.
8. **Thread/Async Safety** — always use `asyncio.Lock` when modifying shared mutable resources (`_INGEST_LOCK`, `_scan_lock`, `_log_lock`).
9. **Multi-Instance Support** — respect `DATA_DIR` so changes remain compatible with multi-instance deployments (`docker-compose.instance2.yml`).
10. **Frontend error contract** — `API.request` toasts server errors once and marks the rejection `err.handled = true`. Callers must not toast again; wrap mutating handlers in `try/catch` or the promise rejection is unhandled.
11. **Module-level caches** — any service cache in a module global needs an invalidation hook and must be reset by `tests/conftest.py`, or tests leak state into each other.