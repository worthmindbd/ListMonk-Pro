# ListMonk Pro — Agent & Developer Guide

A modern, high-performance management dashboard and automation suite for self-hosted [ListMonk](https://listmonk.app/) instances with dual operating modes: **Newsletter** and **Cold Email**. Built with FastAPI (Python 3.12+) and vanilla JavaScript.

---

## Project Structure

```
ListMonk-Pro/
├── app/
│   ├── __init__.py
│   ├── auth.py                    # Session auth, HMAC-SHA256 cookie signing & verification
│   ├── config.py                  # Environment config, .env loader, DATA_DIR resolver
│   ├── main.py                    # FastAPI application, auth middleware, background task lifespan
│   ├── routers/                   # API route handlers
│   │   ├── __init__.py
│   │   ├── appsettings.py         # Installation-wide mode selection state (onboarding gate)
│   │   ├── bounces.py             # Bounce management, manual ingest, filtered exports, deletions
│   │   ├── campaigns.py           # Campaign CRUD, stats, analytics, viewer/clicker/bounce exports
│   │   ├── converter.py           # CSV column detection, conversion & direct ListMonk import
│   │   ├── deliverability.py      # Deliverability audit checklist & 1-click preset switching
│   │   ├── lists.py               # List CRUD endpoints
│   │   ├── replies.py             # Prospect reply monitoring, sentiment filters, status updates
│   │   ├── subscribers.py         # Subscriber/Lead CRUD, memberships, blocklisting, exports
│   │   ├── templates.py           # Template CRUD & default template selection
│   │   └── unsubscribes.py        # Unsubscribe log, campaign groupings, scan trigger, reset/undo
│   └── services/                  # Core business logic & integrations
│       ├── __init__.py
│       ├── app_settings.py        # Installation-wide app settings (chosen mode persisted to DATA_DIR)
│       ├── auto_unblock.py        # Auto-unblock bounce-blocklisted engaged contacts (never opt-outs)
│       ├── bounce_filters.py      # Smart bounce filtering (excludes campaign openers as false positives)
│       ├── bounce_ingest.py       # RFC 3463 DSN classifier, IMAP scanner & processed-ID state
│       ├── bounce_list.py         # Paginated bounce listing with opener exclusion & cache integration
│       ├── bulk_ops.py            # ID validation + bounded-concurrency runner for bulk delete/archive
│       ├── campaign_scheduler.py  # Timezone-aware send window scheduler (auto-pause/auto-resume, _SCHED_LOCK-serialized)
│       ├── csv_converter.py       # Converts arbitrary CSV to ListMonk schema (JSON attributes)
│       ├── deliverability_service.py # Audit rules, score computation, and preset application
│       ├── export_service.py      # CSV generators (sync list + async streamed, formula-injection sanitized)
│       ├── hard_bounce_cache.py   # In-memory hard bounce counts per campaign with background refresh
│       ├── imap_helpers.py        # Shared IMAP utilities (body extraction, date formatting, query escaping,
│       │                          #   RFC 3834 auto-reply headers, bulk-mail detection, campaign-echo stripping)
│       ├── imap_reply_tracker.py  # IMAP reply monitor, sentiment analysis, quote-thread stripping, Message-ID fingerprint dedup,
│       │                          #   HTML-only body fallback + repair pass; routes inbox DSNs into the bounce pipeline (never stored as replies)
│       ├── imap_unsubscribe.py    # IMAP reply monitor for unsubscribe keywords with quote stripping
│       ├── link_unsubscribe.py    # ListMonk direct link unsubscribe scanner & campaign attributor
│       ├── listmonk_client.py     # Async HTTP client for ListMonk REST API (with paginate_all helper)
│       ├── opener_cache.py        # In-memory campaign opener email cache with TTL & inflight deduplication
│       ├── search_query.py        # Safe free-text → ListMonk ILIKE query builder (never raw `query`)
│       ├── reply_log.py           # Persistent JSON storage for prospect replies and sentiments
│       ├── task_utils.py          # Strong references for fire-and-forget tasks & shutdown drain
│       └── unsubscribe_log.py     # Persistent JSON state management for unsubscribe log/settings/processed
├── static/
│   ├── css/
│   │   └── style.css              # Dark theme CSS, segmented mode switcher, reply feed & cards
│   ├── js/
│   │   ├── analytics.js           # Campaign analytics charts, date filtering, link tracking
│   │   ├── api.js                 # Centralized fetch wrapper with 401 redirect & error handling
│   │   ├── app.js                 # SPA router, mode switcher (incl. nav-group collapsing), onboarding modal, shared UI
│   │   │                          #   + App.selection: per-table multi-select + bulk action bars
│   │   ├── campaigns.js           # Campaign management (incl. plain-text alt body), status transitions, test sends
│   │   ├── charts.js              # Mode-aware dashboard overview charts & stat summary cards
│   │   ├── converter.js           # CSV converter UI, column mapping, live preview & import modal
│   │   ├── deliverability.js      # Deliverability pre-flight audit checklist & 1-click presets
│   │   ├── lists.js               # List management, modal forms, tag pills, subscriber drill-down & exports
│   │   ├── replies.js             # Prospect reply feed, sentiment badges, hot lead flagging, bulk triage
│   │   ├── settings.js            # Settings UI: send window scheduler, auto-unblock, unsubscribe settings
│   │   ├── subscribers.js         # Subscriber/lead table: filters, edit modal, bulk list/blocklist/delete
│   │   └── unsubscribes.js        # Campaign-grouped unsubscribe dashboard, source badges, bulk removal, undo reset
│   ├── favicon.png
│   └── favicon.svg
├── templates/
│   ├── index.html                 # Main authenticated SPA shell: categorized sidebar nav (5 groups), mode switcher & onboarding
│   └── login.html                 # Login page with dark theme
├── tests/
│   ├── __init__.py
│   ├── test_analytics.py          # Analytics endpoint & export tests
│   ├── test_bounce_classify.py    # RFC 3463 bounce classifier unit tests
│   ├── test_bounce_filters.py     # Opener exclusion & false positive filter tests
│   ├── test_link_unsubscribe.py   # Link unsubscribe scanner & dedup tests
│   ├── test_bounce_ingest.py      # Bounce DSN idempotency, delay filtering & recipient parsing
│   ├── test_bounce_list.py        # Bounce pagination + type filtering
│   ├── test_campaign_scheduler.py # Send-window logic
│   ├── test_csv_converter.py      # Encoding/delimiter/column detection
│   ├── test_replies_deliverability.py # Reply sentiment, reply API & deliverability preset tests
│   ├── test_bulk_actions.py       # Bulk select endpoints: ID validation, scoping, partial failures
│   ├── test_service_hardening.py  # IMAP timeouts, DSN/reply fingerprint dedup, batch flush, scheduler lock, CSV sanitizer, state caps
│   ├── test_review_fixes.py       # Regression tests for the full-codebase review
│   ├── test_reply_body_extraction.py # HTML-only reply body fallback & stored empty-reply repair regressions
│   ├── test_full_review_fixes.py  # Regressions for the second full-codebase review (CSV blank headers, archive body, scan aborts, state races)
│   ├── test_fourth_review_fixes.py # Regressions for the fourth review (bounce totals, auto-unblock scope, CSRF, unsubscribe scan vs \Seen, bulk payloads)
│   ├── test_reply_filtering.py    # Inbox filtering: sentiment corpus, quote/echo stripping, auto-reply headers, not-a-reply gate
│   ├── conftest.py                # Resets module-level caches between tests
│   └── test_regressions.py        # Route order, IMAP seen-marking, prune, timestamp regressions
├── .github/
│   └── workflows/
│       └── deploy.yml             # Pytest-gated SSH automated deploy for VPS
├── docs/
│   └── superpowers/               # Design specs and execution plans
├── .dockerignore
├── .env.example                   # Template for environment configuration
├── .gitignore
├── DESIGN.md                      # UI design system and token specification
├── Dockerfile                     # Production non-root container definition
├── docker-compose.yml             # Production Docker compose
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
- **State & Storage**: Writable JSON state files in `DATA_DIR` (`schedule.json`, `unsubscribe_log.json`, `unsubscribe_settings.json`, `unsubscribe_processed.json`, `unsubscribe_examined.json`, `bounce_processed.json`), in-memory caches (`opener_cache`, `hard_bounce_cache`)
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
- 7-day session expiration. Tokens are `<issued-at>.<nonce>:<signature>`; the signing key is derived from `SESSION_SECRET` **and** `DASHBOARD_USER`/`DASHBOARD_PASS`, so changing the credentials (and restarting) logs out every session. `POST /auth/logout` revokes the token's nonce in `session_revocations.json` (DATA_DIR, expired entries pruned), so a copied cookie stops working when its owner logs out.
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
- `CSRFMiddleware` rejects state-changing `/api/*` requests unless they carry `X-Requested-With: XMLHttpRequest` or a `Content-Type` whose MIME essence is exactly `application/json`. `multipart/form-data` is **not** exempt (a plain HTML form can send it without a preflight); the SPA's uploads pass because `API.request` always sends `X-Requested-With`.

### 3. Bounce Ingestion & Smart Filtering
- **Bounce Ingestion** (`app/services/bounce_ingest.py`):
  - Connects to bounce IMAP mailbox (`BOUNCE_IMAP_*`) and scans **all DSNs in the last
    `BOUNCE_SCAN_DAYS` (30) days** — not just unseen ones. The mailbox is shared with a
    human, so `\Seen` cannot mean "already ingested": a DSN opened in webmail would
    otherwise be invisible forever.
  - Idempotency comes from `bounce_processed.json` in `DATA_DIR` (a capped list of
    Message-IDs, with a `From|Date|Subject` fingerprint fallback for DSNs that lack
    one). Only the identity headers (`MESSAGE-ID FROM DATE SUBJECT`) are fetched to
    test that set, so already-handled DSNs cost no body download, and the
    fingerprint fallback genuinely differentiates Message-ID-less DSNs. A message
    with no identity material at all is **skipped unprocessed** (never ingested
    without an idempotency key) and retried next scan. A failed `create_bounce` is
    deliberately **not** recorded, so the next scan retries.
  - Handled IDs are flushed to `bounce_processed.json` in batches of
    `PROCESSED_FLUSH_BATCH` (10) **before** messages are marked `\Seen`, plus a
    final flush at scan end — a mid-scan crash can duplicate at most one batch
    instead of re-ingesting the whole scan.
  - All IMAP connections are created with `timeout=settings.imap_timeout`
    (`IMAP_TIMEOUT`, default 60s): a hung mailbox server must not pin an executor
    thread and the module scan lock forever.
  - A DSN that cannot be attributed is discarded **only when the campaign history was
    actually available**. If `_fetch_campaigns_covering` failed or was truncated, those
    DSNs stay unprocessed for the next scan — otherwise a transient ListMonk hiccup at
    container start would silently lose every bounce in the window forever.
  - Classifies into **hard** vs **soft** bounces using RFC 3463 enhanced status codes (e.g. `5.1.1`, `5.1.2`, `5.2.1`) and failure patterns.
  - Skips `Action: delayed` notices entirely — the message is still in flight and a real
    failure arrives as its own DSN.
  - Attributes bounces to campaigns via message headers (`X-Listmonk-Campaign`, `List-ID`) or list membership and dates.
  - Reads the recipient from a `message/delivery-status` part **or** from inline
    `Final-Recipient:` lines, so relays that send bare-text DSNs are handled too.
  - Skips recipients who opened the campaign (cannot be genuine hard bounces).
  - Creates matching bounce records in ListMonk via API.
  - ListMonk's own bounce processing (`bounce.mailboxes`, provider webhooks) is separate:
    the `Bounce Feedback Loop Configured` audit check reports whether *ListMonk* can
    blocklist hard bounces by itself.
- **Opener Cache** (`app/services/opener_cache.py`):
  - In-memory cache of subscribers who opened a campaign (`campaign_views`), with a 10-minute TTL, in-flight request deduplication, and opportunistic eviction of expired entries (a long-running installation used to grow it without bound).
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
  - Scans primary inbox (`IMAP_*`) every hour for reply keywords (`"remove me"`, `"unsubscribe me"`, `"exclude me"`) in **every** message of the lookback window, read or unread — a request the human already opened in webmail must still be honoured (rule 13). A batched identity-header peek (`imap_helpers.fetch_state_ids`) skips messages already recorded in `unsubscribe_examined.json` (Message-ID / `From|Date|Subject` fingerprint, capped at 50,000) without downloading them; bodies are fetched with `BODY.PEEK[]` so examining never flips a message to read. On the first scan of a version with the examined set (file absent) the set is seeded once from messages already `\Seen` — the old scanner flagged everything it looked at, and "Undo All" relied on that, so upgrading never re-processes history. Blocking IMAP calls run via `asyncio.to_thread`.
  - Quote stripping (`_extract_reply_only`): ignores quoted history/template footers to eliminate false positives.
  - List-aware campaign matching: attributes replies to the latest campaign that targeted the subscriber's actual lists.
  - Actions: unenrolls subscriber from all lists, conditionally blocklists (if enabled in settings), logs to `unsubscribe_log.json`.
  - Only messages that were acted on are marked `\Seen`, after their records are persisted — every other message keeps the read state the human left it in. A transient failure (ListMonk down) records nothing, so the next scan retries it. Handled emails are also stored in `unsubscribe_processed.json` (capped at 50,000 entries — the link scanner re-polls ListMonk, where an unsubscribed subscriber stays unsubscribed forever, so an evicted email would be re-processed every hour), so clearing/deleting log records does not cause re-processing.
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
| `auto_unblock_loop` | 6 hours | Finds blocklisted clickers/openers **that have a bounce record** (the blocklisting came from a bounce), re-enables them, deletes the false bounce records. Addresses in the unsubscribe log / processed set are never re-enabled, and blocklistings without a bounce (manual, reply, import) are left alone |
| `scheduler_loop` | 60 seconds | Enforces daily send windows, auto-pauses/resumes campaigns. While **disabled** it resumes the campaigns it auto-paused itself (still-paused ones only; a failed resume stays tracked and is retried), and `PUT /api/scheduler` ticks immediately in both directions. The whole load → mutate → save cycle of a tick runs under `_SCHED_LOCK`, shared with `update_schedule_settings()` (the API's PUT path), so overlapping ticks or a settings write during a tick can never clobber `schedule.json` |
| `imap_scan_loop` | 1 hour | Scans IMAP inbox for replies + polls ListMonk for link unsubscribes |
| `bounce_ingest_loop` | 1 hour | Scans the bounce mailbox for new DSNs in the 30-day window, classifies & records bounces; skips delay notices and anything already in `bounce_processed.json` |
| `reply_scan_loop` | `scan_interval_minutes` (15) | Scans IMAP for cold-outreach prospect replies, strips quotes, classifies sentiment (see *Reply classification* below). Delivery-failure DSNs (mailer-daemon subject/sender/body markers) are never stored as replies: they are routed through `bounce_ingest.ingest_parsed_dsn` so the failed prospect gets a real bounce record (Bounces page, Analytics bounce counts), deduped against `bounce_processed.json` so a mailbox both scanners watch yields one record. HTML-only replies (no usable `text/plain` part) are rendered to text via `imap_helpers.html_to_text` so genuine replies are never stored with an empty body; records already stored empty are repaired in place from the fetched messages, and a re-classification pass re-tags stored replies when the sentiment rules evolve (a DSN stored by an older version is removed from the feed outright). Dedup uses the Message-ID or, when absent, the same `From|Date|Subject` fingerprint as bounce ingestion, persisted to `reply_processed.json` (capped at 5000) |
| `start_cache_updater` | 5 minutes | Refreshes in-memory hard bounce counts per campaign |

### 5a. Reply Classification (`app/services/imap_reply_tracker.py`)
- **Only answers to our outreach are stored.** Newsletter/list mail (`List-Unsubscribe`,
  `List-Id`, `Precedence: bulk|list|junk` without `In-Reply-To`/`References`) is skipped,
  and so is mail whose sender is not a ListMonk subscriber *and* whose subject matches no
  campaign. Skips are terminal (`not_reply` in scan stats, recorded in `reply_processed.json`);
  a failed subscriber lookup is **deferred** (`deferred`), never treated as "unknown sender".
- **Quote stripping** (`imap_unsubscribe._extract_reply_only`, shared with the unsubscribe
  scanner) recognises Outlook `From:`/`Sent:` blocks without an address or leading digit, in
  common locales (`Von:/Gesendet:`, `De :/Envoyé :` …), localised attribution lines
  (`a écrit :`, `schrieb`, `escribió` …), localised "Original Message" separators, mobile
  signatures and the `-- ` delimiter.
- **Campaign-echo backstop**: once a reply is matched to a campaign, any line repeating that
  campaign's body / alt body / template (`imap_helpers.strip_campaign_echo`) is dropped before
  classifying. The unsubscribe scanner applies the same check before acting, so a quoted
  "reply 'remove me'" footer can never unsubscribe the sender. Short footer lines
  ("Unsubscribe") are only dropped inside a block of quoted copy — a one-word answer stands.
- **Order**: bounce → automated → strong refusal → explicit request → soft decline → weaker
  interest cue → `neutral`. Automated evidence from **headers** (`Auto-Submitted`,
  `X-Autoreply`, `Precedence: auto_reply`; stored as `auto_header`), subject prefixes,
  automated senders (`no-reply` anywhere in the local part, helpdesk domains) and explicit
  machine notices is decisive. **Body-only** away / acknowledgement / departure wording yields
  to an explicit human signal (`_HUMAN_OVERRIDE_PATTERNS`: "interested", "let's talk",
  "yes please" …) — never to calls, meetings or pricing, which vacation notices mention.
- Re-classification re-strips stored bodies with the current rules and restores campaign
  attribution (`reattribute_replies`) for records that stop being `auto_reply`.
- `auto_tag_leads=false` keeps positive replies `unread` instead of promoting them to `lead`.

### 6. CSV Converter (`app/services/csv_converter.py`)
- Detects column headers and sample data from uploaded CSVs. Delimiter detection
  scores `,` `\t` `;` `|` on how consistently they split the first rows, using
  `csv.Sniffer` only as a tie-breaker — plain European `a;b;c` exports used to
  collapse into one column.
- Suggests the email/name columns (`detect_columns` → `suggested`): header names first
  (`Email Address`, `Full Name`, `E-MAIL`, …), then the sample values themselves (the
  column that actually holds addresses wins over an unrecognised header).
- Reports rows whose field count differs from the header (`ragged_rows` + `warnings`) so a
  shifted row is visible in the preview instead of silently importing wrong values.
- Maps custom columns to ListMonk's required structure: `email`, `name`, `attributes` (JSON).
  ListMonk's importer only understands those three headers, so with `auto_attributes=True`
  (the default) every column that is not the email/name column is promoted into the
  `attributes` JSON — an arbitrary CSV is preserved without manual mapping. Callers can
  send `attribute_mode=selected` plus an explicit `attribute_columns` list to convert only
  the checked columns; a bare `attribute_columns` list without a mode is treated as
  `selected` for backwards compatibility.
- **A mapping that would drop every unmapped column is rejected, not converted.**
  `attribute_mode=selected` with no usable attribute column (nothing checked, or every
  picked name absent from the file — `unresolved_columns` in stats) returns a
  `no_attributes` error instead of producing a file of empty attributes: ListMonk imports
  that "successfully" while silently storing `{}` for every subscriber. An e-mail-only
  source file is the one legitimate empty-attributes case and is allowed.
- Stats carry what the UI needs to state the outcome honestly: `attribute_count`
  (attributes per row), `attributes_empty`, `empty_attributes` (rows whose attribute
  columns were all blank) and `unresolved_columns`.
- Cell values that are themselves JSON objects (`{"age": 30}`) are merged into the row's
  attributes rather than stringified, so nested attribute paths keep working.
- Rows with a missing or malformed e-mail are skipped and counted
  (`empty_emails` / `invalid_emails` in stats); ListMonk would drop them at import
  anyway, so reporting them beats a misleading "converted" count. Addresses are
  lowercased like ListMonk does, and the accepted shape matches the importer
  (a dotted domain is required, single-label domains are flagged).
- Supports direct import into ListMonk with list selection and subscription mode (`subscribe`,
  `blocklist`). `POST /api/converter/preview` runs the same conversion and returns the
  first rows as JSON, so the UI previews the exact output ListMonk will receive.

### 7. Persistent Runtime State (`DATA_DIR`)
- `app/config.py` provides `settings.data_path(filename)` to store mutable JSON state files (`schedule.json`, `app_settings.json`, `unsubscribe_log.json`, `unsubscribe_settings.json`, `unsubscribe_processed.json`, `unsubscribe_examined.json`, `reply_log.json`, `reply_processed.json`, `reply_settings.json`, `bounce_processed.json`, `session_revocations.json`).
- `bounce_processed.json` is a capped list of handled DSN Message-IDs (newest first) that makes bounce ingestion idempotent independently of IMAP `\Seen`. Its cap (50,000) is much larger than the other sets because bounce ingestion re-examines the whole 30-day window on every scan: a smaller cap started evicting ids of DSNs still inside the window and re-created them as duplicate bounce records. `unsubscribe_examined.json` and `unsubscribe_processed.json` use the same 50,000 cap for the same reason (the IMAP scanner re-reads its whole window; the link scanner re-polls ListMonk). `reply_processed.json` is capped at 5000 entries (oldest evicted), which is safe there — the reply scan only re-reads the 200 most recent messages. The reply scan peeks identity headers first and downloads only unprocessed messages (plus processed ones awaiting a body repair).
- Every JSON state read/write is explicit UTF-8; writes are atomic (tempfile + `fsync` + `os.replace`). `app_settings.load_settings` also guards against a hand-edited file holding a non-object (falls back to defaults instead of 500-ing the onboarding gate).
- In Docker, `DATA_DIR` defaults to `/data` (backed by a named volume).
- Automatically migrates legacy root JSON files into `DATA_DIR` on first run.

---

## API Endpoints Overview

| Category | Endpoint | Method | Description |
|----------|----------|--------|-------------|
| **App** | `/api/app-settings` | GET / PUT | Installation-wide mode state; `mode_selected` gates the onboarding modal (shows once per installation, not per browser) |
| **Auth** | `/auth/login` | GET / POST | Login page / authenticate session |
| | `/auth/logout` | POST | End session & redirect (POST-only: a GET would be a forced-logout CSRF) |
| **Subscribers** | `/api/subscribers` | GET | Paginated subscribers with free-text `search` (escaped into an ILIKE on email/name — never pass raw text as `query`, which is a ListMonk SQL passthrough), `query`/list/`status` filter |
| | `/api/subscribers/export-all` | GET | Stream all subscribers as CSV |
| | `/api/subscribers/import/status` | GET | Get subscriber import status |
| | `/api/subscribers/import/logs` | GET | Get subscriber import logs |
| | `/api/subscribers/{id}` | GET / PUT / DELETE | Get / update / delete subscriber |
| | `/api/subscribers/{id}/export` | GET | Export single subscriber JSON profile |
| | `/api/subscribers/{id}/bounces` | GET | Get bounces for subscriber |
| | `/api/subscribers/{id}/blocklist` | PUT | Blocklist single subscriber |
| | `/api/subscribers/blocklist` | PUT | Bulk blocklist subscribers by ID array |
| | `/api/subscribers/lists` | PUT | Bulk modify list memberships (add/remove/unsub) |
| | `/api/subscribers` | POST / DELETE | Create subscriber / bulk delete subscribers by ID array |
| **Lists** | `/api/lists` | GET / POST | List all mailing lists / create new list |
| | `/api/lists` | DELETE | Bulk delete lists by ID array (bounded loop, partial failures reported) |
| | `/api/lists/{id}` | GET / PUT / DELETE | Get / update / delete list |
| **Campaigns** | `/api/campaigns` | GET / POST | List campaigns (with hard bounce cache) / create |
| | `/api/campaigns/running/stats` | GET | Real-time running campaign stats |
| | `/api/campaigns/summary` | GET | Aggregate totals across all campaigns (cached 60s) |
| | `/api/campaigns/export-all` | GET | Export all campaigns summary as CSV |
| | `/api/campaigns/analytics/{type}` | GET | Campaign analytics (views/clicks/bounces/links — the type is whitelisted before it reaches the upstream URL) |
| | `/api/campaigns/analytics/{type}/export` | GET | Export campaign analytics as CSV |
| | `/api/campaigns/{id}` | GET / PUT / DELETE | Get / update / delete campaign |
| | `/api/campaigns/{id}/preview` | GET | Preview campaign HTML rendering |
| | `/api/campaigns/{id}/status` | PUT | Change status (start, pause, cancel) |
| | `/api/campaigns/{id}/archive` | PUT | Archive campaign |
| | `/api/campaigns/archive` | PUT | Bulk archive campaigns by ID array (registered before `/{id}`) |
| | `/api/campaigns` | DELETE | Bulk delete campaigns by ID array |
| | `/api/campaigns/{id}/test` | POST | Send test email |
| | `/api/campaigns/{id}/subscribers/{type}` | GET | View campaign viewers, clickers, or hard bounces |
| | `/api/campaigns/{id}/subscribers/{type}/export` | GET | Export viewers, clickers, or hard bounces as CSV; optional `from_date`/`to_date` (YYYY-MM-DD, strictly validated) bound the export to the window the analytics charts show |
| **Templates** | `/api/templates` | GET / POST | List templates / create template |
| | `/api/templates` | DELETE | Bulk delete templates by ID array |
| | `/api/templates/{id}` | GET / PUT / DELETE | Get / update / delete template |
| | `/api/templates/{id}/default` | PUT | Set default template |
| **Bounces** | `/api/bounces` | GET / DELETE | List bounces (filtered) / bulk delete (`ids` narrows to a selection, `campaign_id`+`bounce_type` widen to a sweep) |
| | `/api/bounces/ingest` | POST | Trigger bounce mailbox IMAP ingestion |
| | `/api/bounces/export` | GET | Export filtered bounce records as CSV |
| | `/api/bounces/{id}` | DELETE | Delete single bounce record |
| **CSV Converter** | `/api/converter/detect-columns` | POST | Detect CSV headers & preview rows |
| | `/api/converter/preview` | POST | Convert and return the first rows as JSON (exact ListMonk output preview) |
| | `/api/converter/convert` | POST | Convert CSV to ListMonk schema (download) |
| | `/api/converter/convert-and-import` | POST | Convert and import directly into ListMonk (`subscription_status` defaults to `confirmed` so double-opt-in lists are mailable; `overwrite_userinfo` / `overwrite_subscription_status` map onto ListMonk's granular upsert flags; rejects `no_attributes` mappings and empty conversions) |
| **Scheduler** | `/api/scheduler` | GET / PUT | Get / update send window settings |
| | `/api/scheduler/run` | POST | Manually trigger scheduler tick |
| **Auto-Unblock** | `/api/auto-unblock/status` | GET | Get count of blocklisted engaged contacts |
| | `/api/auto-unblock/run` | POST | Manually run auto-unblock process |
| **Unsubscribes** | `/api/unsubscribes` | GET | Paginated unsubscribe log |
| | `/api/unsubscribes/stats` | GET | Unsubscribe counts & source breakdown |
| | `/api/unsubscribes/settings` | GET / PUT | Get / update unsubscribe settings (blocklist toggle) |
| | `/api/unsubscribes/campaigns` | GET | Unsubscribes grouped by campaign |
| | `/api/unsubscribes/campaigns` | DELETE | Bulk delete the records of several selected campaigns by ID array |
| | `/api/unsubscribes/campaign/{id}` | GET / DELETE | Get / delete records for specific campaign |
| | `/api/unsubscribes/campaign/{id}/export` | GET | Export campaign unsubscribes as CSV |
| | `/api/unsubscribes/records` | DELETE | Delete specific records by email |
| | `/api/unsubscribes/export` | GET | Export all unsubscribes as CSV |
| | `/api/unsubscribes/scan` | POST | Manually trigger IMAP + link scan (502 when a configured mailbox is unreachable; 200 with an explicit "not configured — link scan only" message otherwise) |
| | `/api/unsubscribes/imap-status` | GET | Check IMAP connection health |
| | `/api/unsubscribes/clear` | DELETE | Clear all unsubscribe log records |
| | `/api/unsubscribes/reset` | POST | Undo all unsubscribes & restore list memberships |
| **Replies** | `/api/replies` | GET | Paginated prospect replies with sentiment/status filtering |
| | `/api/replies/stats` | GET | Aggregate reply counts, reply rate %, and sentiment breakdown (optional `campaign_id` scopes counts and rate to one campaign; a `by_campaign` breakdown is always returned for the analytics chart) |
| | `/api/replies/scan` | POST | Manually trigger IMAP prospect reply scan (502 when the mailbox scan fails — never a 200 "ok" with a buried error) |
| | `/api/replies/status` | GET | Check IMAP connection status |
| | `/api/replies/settings` | GET / PUT | Get / update reply tracker settings: `auto_tag_leads` (positive → Hot Lead), `scan_interval_minutes` (1–1440), `auto_blocklist_machine`, `auto_blocklist_ooo` |
| | `/api/replies/status` | PUT | Bulk update reply status from the feed's selection bar |
| | `/api/replies/blocklist` | POST | Bulk blocklist every selected reply's sender |
| | `/api/replies` | DELETE | Bulk delete replies by ID array |
| | `/api/replies/{id}/status` | PUT | Update reply status (unread, lead, replied, archived) |
| | `/api/replies/{id}/blocklist` | POST | Blocklist the ListMonk subscriber behind a reply (resolved server-side) |
| | `/api/replies/{id}` | DELETE | Delete / dismiss single reply record |
| **Deliverability** | `/api/deliverability/checklist` | GET | Audit Listmonk settings against deliverability rules (10 checks: tracking, individual tracking, unsubscribe header, batch size, concurrency, public archive, bounce feedback loop, hard-bounce action, send cadence, upload allow-list). The one-click `List-Unsubscribe` header is **required** (Gmail/Yahoo bulk-sender rules) and both presets keep it on |
| | `/api/deliverability/apply-preset` | POST | 1-click apply 'cold_email' or 'newsletter' preset (merged onto current settings via `apply_settings`) |

**Scoring rule:** only `pass`/`warn`/`fail` checks count toward the score; `info` checks
(e.g. sending cadence, public archive) are reported for context and never drag the score
down. The audit deliberately covers settings the app does **not** manage (ListMonk bounce
mailboxes, media upload types) so a "100%" rating cannot hide an unconfigured bounce loop.

---

## Data Management (per page)

Every data page exposes row-level actions plus a multi-select bulk bar
(`App.selection` in `app.js`, `.bulk-bar` in `style.css`):

| Page | Row actions | Bulk actions (checkbox column / card checkbox) |
|------|-------------|-----------------------------------------------|
| Leads/Subscribers | View details, Edit (incl. list memberships), Blocklist, Delete | Add/Remove from list, Blocklist, Export Selected (CSV), Delete |
| Lead Lists | View Subscribers (jumps to the filtered Lead table), Export, Edit, Delete | Export Subscribers, Delete |
| Cold Outreach/Campaigns | View, Start/Pause/Resume, Test Send, Archive, Delete | Archive, Export Selected (CSV), Delete |
| Templates | Preview, Duplicate, Edit, Set Default, Delete (search by name) | Delete |
| Bounces | Delete | Export Selected (CSV), Delete Selected (plus the existing filter-wide Delete All) |
| Replies & Leads | Status dropdown, Mark as Hot Lead, Copy Email, Full Details, Blocklist, Delete | Mark Hot Lead / Replied / Archived / Unread, Blocklist, Delete |
| Unsubscribes | per-campaign Remove, Remove List (detail view), record removal | Delete Records for the selected campaign cards |

Frontend conventions:

- A page calls `App.selection.register(key, {noun, pageValues, actions})` on every
  render, places `App.selection.bar(key)` above its table and wires
  `${App.selection.headbox(key, label)}` / `${App.selection.rowbox(key, value, label)}`
  into the markup. Values are held as strings so a selection survives pagination.
- Handlers read `App.selection.values(key)`, act once, then `App.selection.clear(key)`
  and re-render. Selection state is per page and dropped on navigation
  (`App.selection.clearAll()` in `App.route`).
- Client-side CSVs (`Export Selected`) only cover rows loaded on the current page;
  the filter-wide exports still use the streaming server endpoints.

Backend conventions:

- `app/services/bulk_ops.py` provides `parse_ids()` (rejects junk, dedupes, caps at
  `MAX_BULK_IDS = 500`) and `run_bulk()` (bounded concurrency, returns
  `{ok, errors, failed}`) for the endpoints ListMonk only offers per item
  (lists, campaigns, templates).
- Bulk actions must only touch the submitted IDs — never fall back to a
  filter-wide sweep when a selection is present.
- Partial failures are reported in the response body (`errors` / `failed`) and
  are logged, never silently swallowed.
- Static bulk routes (`PUT /api/campaigns/archive`) are registered before the
  parameterized ones (`PUT /api/campaigns/{id}`) or Starlette int-parses the
  literal segment and answers 422.

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
3. **Escaping ListMonk SQL queries** — use `safe_email_for_query` from `imap_helpers.py` when passing user input to ListMonk queries. ListMonk's subscriber `query` parameter is raw SQL: free-text search boxes must go through a builder like `subscribers._search_to_query` (strips LIKE wildcards/backslashes, doubles quotes, wraps in ILIKE), never through `query` verbatim. Any path segment interpolated into an upstream ListMonk URL (e.g. `analytics_type`) must be whitelisted first.
4. **Memory efficiency** — for CSV exports, prefer `aiter_dicts_to_csv` from `export_service.py` fed by `listmonk.iter_pages(...)` so rows stream one page at a time. `dict_list_to_csv` still buffers its input list, so only use it when the dataset is already materialised (e.g. after in-process filtering). Both writers neutralize spreadsheet formula injection (`=`, `+`, `-`, `@`, tab, CR leading characters get a `'` prefix) — client-side "Export Selected" builders must use `App.csvCell()` for the same reason.
5. **Bound pagination inputs** — every endpoint taking `page`/`per_page` must declare `Query(..., ge=1, le=MAX)`. Bounce listing is the expensive one: serving page N scans every preceding record, so an unbounded `per_page` multiplies into tens of thousands of ListMonk queries.
6. **Never chain `.get(k, {}).get(...)`** on ListMonk response fields that can be explicitly null (notably `bounce["campaign"]`). Use the helpers in `bounce_filters.py` (`bounce_campaign_id` / `bounce_campaign_name`).
7. **Failed actions return a status code** — raise `HTTPException` rather than returning `200 {"error": ...}`, so monitoring and the frontend can distinguish success from failure. The one exception is `/api/auto-unblock/status`, which degrades to a `200` with an `error` field because the Settings page loads it via `Promise.all`.
8. **Thread/Async Safety** — always use `asyncio.Lock` when modifying shared mutable resources (`_INGEST_LOCK`, `_scan_lock`, `_log_lock`).
9. **Persistent Storage** — respect `DATA_DIR` for mounted volumes and file storage (`session_secret.key`, `reply_log.json`, etc.).
10. **Frontend error contract** — `API.request` toasts server errors once and marks the rejection `err.handled = true`. Callers must not toast again; wrap mutating handlers in `try/catch` or the promise rejection is unhandled.
11. **Module-level caches** — any service cache in a module global needs an invalidation hook and must be reset by `tests/conftest.py`, or tests leak state into each other.
12. **ListMonk settings are all-or-nothing** — `PUT /api/settings` validates the whole payload (a partial body fails with *"At least one SMTP block should be enabled"*), and the GET response returns every secret masked with `•`. Never PUT a partial dict and never echo a mask back: use `listmonk.apply_settings(patch)`, which merges onto the current settings and blanks masked secrets so ListMonk keeps the stored value (matched by SMTP/bounce UUID).
13. **Never treat IMAP `\Seen` as "already processed"** — the scanners work on mailboxes a human reads. Bounce ingestion persists handled DSN Message-IDs to `bounce_processed.json`; the unsubscribe and reply scanners do the same. New scanners must follow that pattern (or keep an equivalent state file).
14. **Bulk actions only touch the submitted IDs** — never widen a selection into a filter-wide sweep. Validate IDs with `bulk_ops.parse_ids`, run per-item calls through `bulk_ops.run_bulk`, report partial failures (`errors`/`failed`) in the response, and register static bulk routes (`/archive`) before parameterized ones (`/{id}`).