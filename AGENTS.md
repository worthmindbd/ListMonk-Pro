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
│   │   ├── email_validation.py    # Email Validation: quick check, subscriber scan, results list/export/blocklist/dismiss, scheduled-cleanup settings & run-now
│   │   ├── lists.py               # List CRUD endpoints
│   │   ├── replies.py             # Prospect reply monitoring, sentiment filters, status updates
│   │   ├── sends.py               # Daily send statistics ("sent today", sends per day)
│   │   ├── subscribers.py         # Subscriber/Lead CRUD, memberships, blocklisting, exports
│   │   ├── templates.py           # Template CRUD & default template selection
│   │   ├── unsubscribes.py        # Unsubscribe log, campaign groupings, scan trigger, reset/undo
│   │   └── warmup.py              # Sending warm-up: settings, live status, ramp preview, queue/send-speed insights
│   └── services/                  # Core business logic & integrations
│       ├── __init__.py
│       ├── app_settings.py        # Installation-wide app settings (chosen mode persisted to DATA_DIR)
│       ├── auto_unblock_state.py  # Auto-unblock on/off switch, last run and re-enabled history (auto_unblock.json)
│       ├── auto_unblock.py        # Auto-unblock bounce-blocklisted contacts that engaged AFTER the bounce (never opt-outs or deliberate blocklistings)
│       ├── blocklist_registry.py  # Subscriber ids blocklisted on purpose through this app (manual_blocklist.json); auto-unblock never reverses them
│       ├── bounce_filters.py      # Smart bounce filtering (excludes campaign openers as false positives)
│       ├── bounce_ingest.py       # RFC 3463 DSN classifier, IMAP scanner & processed-ID state
│       ├── bounce_list.py         # Paginated bounce listing with opener exclusion & cache integration
│       ├── bulk_ops.py            # ID validation + bounded-concurrency runner for bulk delete/archive
│       ├── campaign_scheduler.py  # Timezone-aware send window scheduler (auto-pause/auto-resume, _SCHED_LOCK-serialized)
│       │                          #   + the fast warm-up quota loop (enforce_warmup / warmup_loop / nudge_warmup)
│       ├── csv_converter.py       # Converts arbitrary CSV to ListMonk schema (JSON attributes)
│       ├── deliverability_service.py # Audit rules, score computation, and preset application
│       ├── email_cleanup.py       # Subscriber scan + scheduled "dead email" cleanup: single job slot, safety rails, daily/weekly/monthly slot maths, results store
│       ├── email_format.py        # Per-campaign HTML design ↔ plain text switch (plain twins, text parts, memory in campaign attribs)
│       ├── email_validation.py    # Pure address validator: syntax, DNS MX / Null MX, disposable / role / typo flags, per-domain DNS cache
│       ├── email_validation_data.py # Bundled lists: disposable domains, role local parts, typo domains / TLDs, top providers
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
│       ├── sending_reputation.py  # Sending IPs (SMTP hosts or SENDING_IPS), reverse DNS (FCrDNS + alignment), DNSBLs (test-entry verified), SPF evaluator, DKIM/DMARC, 7-day sender-side rejections; audit rows + dashboard alerts
│       ├── send_stats.py          # Always-on per-day send counter (delta-counted from running stats, shared poll with warm-up) + in-memory stall tracking of running campaigns
│       ├── search_query.py        # Free-text search builders: escaped ILIKE for subscribers (SQL), plain_search_term for lists/campaigns (plain text)
│       ├── reply_i18n.py          # Non-English reply vocabulary (ack / refusal / interest / dead-address) merged into the classifier, plus opt-out phrases merged into the unsubscribe scanner
│       ├── reply_log.py           # Persistent JSON storage for prospect replies and sentiments
│       ├── task_utils.py          # Strong references for fire-and-forget tasks & shutdown drain
│       ├── template_unsubscribe.py # Footer unsubscribe link toggle: reversibly hides/restores {{ UnsubscribeURL }} in campaign templates
│       ├── unsubscribe_log.py     # Persistent JSON state management for unsubscribe log/settings/processed
│       └── warmup.py              # Warm-up ramp maths, per-day quota accounting from live ListMonk send counts, day roll-over
├── static/
│   ├── css/
│   │   └── style.css              # Dark theme CSS, segmented mode switcher, replies inbox (list + reading pane)
│   ├── js/
│   │   ├── analytics.js           # Campaign analytics charts, date filtering, link tracking
│   │   ├── api.js                 # Centralized fetch wrapper with 401 redirect & error handling
│   │   ├── app.js                 # SPA router, mode switcher (incl. nav-group collapsing), onboarding modal, shared UI
│   │   │                          #   + App.selection: per-table multi-select + bulk action bars
│   │   ├── campaigns.js           # Campaign management (incl. plain-text alt body), status transitions, test sends
│   │   ├── charts.js              # Mode-aware dashboard overview charts & stat summary cards
│   │   ├── converter.js           # CSV converter UI, column mapping, live preview & import modal
│   │   ├── deliverability.js      # Deliverability audit with a control per setting (staged edits + save bar), 1-click presets
│   │   │                          #   & per-campaign Email format switch (HTML design / Plain text)
│   │   ├── lists.js               # List management, modal forms, tag pills, subscriber drill-down & exports
│   │   ├── replies.js             # Replies inbox: sentiment tabs, list + pinned reading pane, hot lead flagging, bulk triage
│   │   ├── settings.js            # Settings UI: send window scheduler, sending warm-up (plan form, live meter, ramp chart), email cleanup schedule, auto-unblock
│   │   ├── subscribers.js         # Subscriber/lead table: filters, edit modal, bulk list/blocklist/delete
│   │   ├── unsubscribes.js        # Campaign-grouped unsubscribe dashboard, source badges, bulk removal, undo reset
│   │   └── validation.js          # Email Validation page: quick check, scan card with live progress, issues table + bulk bar
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
│   ├── test_real_world_replies.py # Replies mis-filed on a live mailbox, multilingual corpus, precision guard, campaign-only gate + prune, header backfill
│   ├── test_deliverability_controls.py # Per-setting edits (whitelist/validation) & footer unsubscribe link template toggle
│   ├── test_warmup.py             # Warm-up: ramp maths, delta counting, day roll-over/advance rule, quota enforcement, scheduler interplay, API
│   ├── test_email_format.py       # HTML ↔ plain switch: twin/text conversion, remembered setups, compile rollback, attribs kept on edit
│   ├── test_email_validation.py   # Validator (syntax, DNS verdicts, flags, cache), scan, cleanup safety rails, schedule maths, API
│   ├── test_send_stats_and_ui_fixes.py # Daily send counter, unattributed unsubscribe group, unblock, template preview
│   ├── test_ui_review_round2.py   # Unsaved campaign/template previews, reply timeline, auto-unblock switch/history, validate-before-import
│   ├── test_sending_reputation.py # Reputation: rDNS mismatch, DNSBL listed / refusing lists, SPF evaluation, DKIM/DMARC, rejections per provider, classifier sender tags, cache, API
│   ├── test_send_pacing_fixes.py  # Live sending incident: stall detection, window rate 1 = 2/window, batch vs window, deleted-campaign "sent today", hidden template text
│   ├── test_fifth_review_fixes.py # Regressions for the fifth review (plain-text search, IP octets vs status codes, DSN gate, keyword boundaries, blocklist registry, subject matching, mode-aware audit, auth cross-site)
│   ├── conftest.py                # Resets module-level caches between tests
│   └── test_regressions.py        # Route order, IMAP seen-marking, prune, timestamp regressions
├── .github/
│   └── workflows/
│       └── deploy.yml             # Pytest-gated SSH deploy to every VPS in secret DEPLOY_TARGETS ("user@host dir…" per line) → reset + deploy.sh per dir;
│                                  #   no server is ever named in the repo (it is public: hosts/paths are masked in logs)
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
├── deploy.sh                      # Rebuild + health-check the instance in this checkout (port/name from its .env; --pull to update)
├── start.sh                       # Local development startup script
└── README.md                      # Public documentation
```

---

## Technology Stack

- **Backend**: Python 3.12+, FastAPI 0.141+, Uvicorn, httpx (async HTTP), dnspython (async MX lookups), Jinja2, Python-dotenv
- **Frontend**: Vanilla JavaScript (ES6+), Chart.js (vendored in `static/js/vendor/`), Modern Dark CSS (no build step)
- **State & Storage**: Writable JSON state files in `DATA_DIR` (`schedule.json`, `warmup.json`, `send_stats.json`, `auto_unblock.json`, `email_cleanup.json`, `email_validation_results.json`, `unsubscribe_log.json`, `unsubscribe_settings.json`, `unsubscribe_processed.json`, `unsubscribe_examined.json`, `bounce_processed.json`), in-memory caches (`opener_cache`, `hard_bounce_cache`)
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
- `POST /auth/login` and `POST /auth/logout` sit outside `/api`, so they carry their own check: a request labelled `Sec-Fetch-Site: cross-site` is refused (403), and login requires a JSON content type (415).
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
  - **Only delivery-status notices are ingested** (`looks_like_dsn`: `multipart/report` / `message/delivery-status`, `X-Failed-Recipients`, a mailer-daemon sender, RFC 3464 fields or the MTAs' failure wording). The bounce address is the envelope sender, so it also receives out-of-office notices, ticket acknowledgements and ordinary mail; without the gate those were parsed as bounces against whatever address sat in angle brackets in the body. Anything else is recorded as handled (`not_a_dsn`).
  - Classifies into **hard** vs **soft** bounces using RFC 3463 enhanced status codes (e.g. `5.1.1`, `5.1.2`, `5.2.1`) and failure patterns. Status-code patterns must start at a boundary (`(?<![\d.])`): the octets of an IP address in the DSN (`185.1.1.20`) are not a `5.1.1`.
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
  - Scans primary inbox (`IMAP_*`) every hour for reply keywords (`"remove me"`, `"unsubscribe me"`, `"exclude me"` and their plural-subject forms `"remove us"`, `"unsubscribe us"`, `"exclude us"` — B2B prospects answer for their company) plus other plain instructions to stop the mail — `"please unsubscribe"`, `"stop emailing me"`, `"take us off your list"`, `"do not contact us"`, `"no further contact"`, a reply that is just `"STOP"`/`"Unsubscribe"` — and their equivalents in DE/FR/ES/IT/PT/NL/PL/DA/NO/SV (`reply_i18n.OPT_OUT_PATTERNS`). Only an *instruction aimed at the sender's own address* counts; a refusal ("not interested", `"nie jestem zainteresowany"`) does not unsubscribe, and neither do bare footer words (`"unsubscribe"`, `"désinscrire"`). Mail carrying RFC 3834 automation headers (`Auto-Submitted`, `X-Autoreply`, `Precedence: auto_reply`) is never acted on: a responder's footer is boilerplate, and acting on it would unenrol a real prospect who is merely away. The scan covers **every** message of the lookback window, read or unread — a request the human already opened in webmail must still be honoured (rule 13). A batched identity-header peek (`imap_helpers.fetch_state_ids`) skips messages already recorded in `unsubscribe_examined.json` (Message-ID / `From|Date|Subject` fingerprint, capped at 50,000) without downloading them; bodies are fetched with `BODY.PEEK[]` so examining never flips a message to read. On the first scan of a version with the examined set (file absent) the set is seeded once from messages already `\Seen` — the old scanner flagged everything it looked at, and "Undo All" relied on that, so upgrading never re-processes history. Blocking IMAP calls run via `asyncio.to_thread`.
  - The literal keywords are word-bounded ("remove mention", "exclude media", "remove users" are not opt-outs). A sender already in `unsubscribe_processed.json` is skipped only while they still have no active subscription (`_has_active_subscription`): someone re-subscribed since (a re-import) who asks again is honoured. A request from an address that is not a subscriber cannot be acted on; it is logged as a warning and counted (`unmatched` in the scan result).
  - Quote stripping (`_extract_reply_only`): ignores quoted history/template footers to eliminate false positives.
  - List-aware campaign matching: attributes replies to the latest campaign that targeted the subscriber's actual lists.
  - Actions: unenrolls subscriber from all lists, conditionally blocklists (if enabled in settings), logs to `unsubscribe_log.json`.
  - Only messages that were acted on are marked `\Seen`, after their records are persisted — every other message keeps the read state the human left it in. A transient failure (ListMonk down) records nothing, so the next scan retries it. Handled emails are also stored in `unsubscribe_processed.json` (capped at 50,000 entries — the link scanner re-polls ListMonk, where an unsubscribed subscriber stays unsubscribed forever, so an evicted email would be re-processed every hour), so clearing/deleting log records does not cause re-processing.
- **Link Unsubscribe Scanner** (`app/services/link_unsubscribe.py`):
  - Polls ListMonk lists for subscribers with `unsubscribed` status. Subscribers whose own status is `blocklisted` are skipped: ListMonk marks every subscription of a blocklisted subscriber `unsubscribed`, so a bounce / manual / cleanup blocklisting would otherwise be logged as a link unsubscribe (and re-enabled by "Undo All").
  - Cascades unsubscription across all lists and applies optional blocklist.
  - Logs records with `source: "link"`; serialized with a module lock so manual and background scans cannot double-process.
- **Undo / Reset** (`POST /api/unsubscribes/reset`):
  - Re-subscribes users to their original removed lists, resets status to `enabled`, removes restored records from log, and unmarks their emails so future unsubscribes are detected again. A blocklisting the engine did not cause is never lifted: records carry `blocklisted` (did the scanner blocklist?), and for older records a still-blocklisted subscriber with a bounce record is skipped and stays in the log.

### 5. Automation Workers (FastAPI Lifespan)
All background tasks run as managed `asyncio.create_task` loops in `app/main.py`:
| Task Loop | Interval | Purpose |
|-----------|----------|---------|
| `auto_unblock_loop` | 6 hours (skipped while switched off in Settings → Auto-Unblock; every run's outcome and the re-enabled addresses go to `auto_unblock.json` via `auto_unblock_state.record_run`) | Finds blocklisted subscribers with a bounce record **who opened or clicked after their latest bounce** (`QUERY_BLOCKLISTED_ENGAGED`; engagement *before* a bounce proves nothing: the mailbox may have died since), re-enables them, deletes the false bounce records. Never re-enabled: addresses in the unsubscribe log / processed set, and ids in `blocklist_registry` (`manual_blocklist.json`): every blocklisting done through this app (Leads / Replies / Email Validation actions, reply auto-blocklist, cleanup) is recorded there, because ListMonk does not store *why* a subscriber is blocklisted. A new code path that blocklists on purpose must call `blocklist_registry.mark(ids)` |
| `scheduler_loop` | 60 seconds | Enforces daily send windows, auto-pauses/resumes campaigns. While **disabled** it resumes the campaigns it auto-paused itself (still-paused ones only; a failed resume stays tracked and is retried), and `PUT /api/scheduler` ticks immediately in both directions. The whole load → mutate → save cycle of a tick runs under `_SCHED_LOCK`, shared with `update_schedule_settings()` (the API's PUT path), so overlapping ticks or a settings write during a tick can never clobber `schedule.json` |
| `imap_scan_loop` | 1 hour | Scans IMAP inbox for replies + polls ListMonk for link unsubscribes |
| `bounce_ingest_loop` | 1 hour | Scans the bounce mailbox for new DSNs in the 30-day window, classifies & records bounces; skips delay notices and anything already in `bounce_processed.json` |
| `warmup_loop` | 1 s while warm-up is on and a campaign is running, 3 s otherwise (backs off to 30 s when ListMonk is unreachable) | Every iteration fetches `/api/campaigns/running/stats` **once** and feeds it to the daily send statistics (`send_stats.observe`, always on, failures logged and never blocking the quota) and to warm-up. While warm-up is **enabled** it counts what ListMonk has sent since the last poll (`/api/campaigns/running/stats`) and pauses every running campaign the moment today's warm-up quota is used up (see *Sending warm-up* below). A new day closes the previous one and runs a scheduler tick so held-back campaigns resume at once. Starting/resuming a campaign through `PUT /api/campaigns/{id}/status` wakes it immediately (`nudge_warmup`) |
| `reply_scan_loop` | `scan_interval_minutes` (15) | Scans IMAP for cold-outreach prospect replies, strips quotes, classifies sentiment (see *Reply classification* below). Delivery-failure DSNs (mailer-daemon subject/sender/body markers) are never stored as replies: they are routed through `bounce_ingest.ingest_parsed_dsn` so the failed prospect gets a real bounce record (Bounces page, Analytics bounce counts), deduped against `bounce_processed.json` so a mailbox both scanners watch yields one record. HTML-only replies (no usable `text/plain` part) are rendered to text via `imap_helpers.html_to_text` so genuine replies are never stored with an empty body; records already stored empty are repaired in place from the fetched messages, and a re-classification pass re-tags stored replies when the sentiment rules evolve (a DSN stored by an older version is removed from the feed outright). Dedup uses the Message-ID or, when absent, the same `From|Date|Subject` fingerprint as bounce ingestion, persisted to `reply_processed.json` (capped at 5000) |
| `start_cache_updater` | 5 minutes | Refreshes in-memory hard bounce counts per campaign |
| `email_cleanup_loop` | 60 seconds (only works when a slot is due) | Runs the scheduled dead-email cleanup (Settings → Email Cleanup, off by default) when a daily / weekly / monthly slot has passed unhandled; see *Email validation & cleanup* below. Backs off on errors like the other loops |

### 5b. Sending Warm-up (`app/services/warmup.py`, `app/services/campaign_scheduler.py`)
- Caps the **total** campaign emails per day (all campaigns share one quota — reputation belongs to the sending domain) and raises the cap gradually: `build_ramp(start, growth_percent, target)` grows each day by the percentage but by at least one email (integer maths, rounded half up) and ends exactly on the target. `100` gives 10, 20, 40, 80 …; presets (`RAMP_PRESETS`: gentle +10 %, standard +20 %, doubling ×2) only fill start/growth.
- **Why the cap lives in this app, not in ListMonk's `message_sliding_window_*`:** ListMonk's window is global, held in memory, a *fixed* window anchored to process start (not to midnight), and its settings only apply after a reload that ListMonk refuses to do while a campaign is running (`needs_restart`). Do not "simplify" warm-up onto those settings.
- **Counting is delta-based:** `observe()` polls `GET /api/campaigns/running/stats` (live in-memory `sent`, ahead of the DB) and adds `sent − seen[campaign]` to `sent_today`; `seen` is persisted in `warmup.json`, so sends made while this app restarted are counted on the next poll. Enabling (or re-enabling) warm-up **baselines** every running/paused campaign first, so a campaign's earlier history is never charged to today. A campaign that leaves the running list is read once more via `GET /api/campaigns/{id}` so its last messages are not lost (`unsettled` keeps a failed lookup for a retry). Network calls run outside `_WARMUP_LOCK`; the write is a fresh load → mutate → save inside it.
- **Enforcement:** `enforce_warmup()` pauses every running campaign when `remaining <= 0` via `_pause_for_quota`, which records them in the scheduler's own `auto_paused_campaigns` (under `_SCHED_LOCK`). The 60 s tick treats "quota used up" like "outside the send window": `may_send = in_window AND quota_left`, where a switched-off rule never blocks. So a campaign paused for either reason resumes only when **neither** applies, manual pauses are never touched, and turning both features off releases everything. Warm-up *alone* works without the send-window scheduler being enabled.
- **Day = calendar day in the warm-up `timezone`** (`today_for`). `_roll_day()` closes the stored day when the date moves on, appends it to `history` (capped at 60) and resets the counter. **The ramp advances only after a day that sent ≥ `ADVANCE_MIN_FRACTION` (80 %) of its quota** — a weekend, an empty queue or an app outage must never push a cold domain to a bigger volume. Only one day is closed however long the app was down. Changing the timezone moves `date` without closing the day. `view()` shows the next day's quota with a zero counter between midnight and the first poll (`pending_rollover`), so `has_quota()` is never stale-false.
- **Known limits (also stated in the UI):** the cap can be exceeded by what ListMonk sends between two polls (≈ 1 s of sending: message rate × concurrency is the upper bound, so the Insights card warns when that is large against the quota); while this app is stopped ListMonk sends without a cap; a send that failed at SMTP is not counted.
- Frontend: Settings → *Sending Warm-up* (`Settings.renderWarmup`). The live meter is refreshed in place by `_syncWarmupStatus()` (never re-rendered, so unsaved form edits survive); the ramp chart is drawn from `/api/warmup/preview` so the maths exists only in Python.

### 5d. Daily Send Statistics (`app/services/send_stats.py`, `GET /api/sends/daily`)
- ListMonk stores only a cumulative `sent` per campaign, never *when* a message left, so "sent today" cannot be read from it later. `send_stats` counts sends as they happen, **warm-up on or off**, with the same delta function as warm-up (`warmup.count_deltas`): the first poll only baselines (`seen`), so a campaign's history is never charged to the day counting starts; campaigns that leave the running list are read once more for their final count.
- Per-day buckets `{total, campaigns: {id: n}}` in `send_stats.json` (≤ 400 days). Day boundary = the warm-up timezone while warm-up is on (so the Analytics card and the warm-up meter agree), else the send-window timezone.
- On first run the file is seeded from the warm-up history (`source: "warmup"`). A day is `tracked` from `counting_since` on (missing = really zero); before that only seeded days are known — the gaps are `tracked: false` (unknown, drawn without a bar), never zero.
- Frontend: Analytics *Sent Today* card + *Daily Sends* chart (7/30/90 days, today split by campaign, deleted campaigns named); Dashboard "N sent today" line (says when it came from a deleted campaign), a "Live from ListMonk · read at …" bar with Refresh, a campaign table showing `sent / to_send`, and a danger item in the attention strip for a stalled running campaign.
- **Stall tracking** (`send_stats.track_progress`, fed by the same poll): in memory only (`_PROGRESS`, `_LAST_POLL`, reset by `reset_progress()` in conftest) — when each running campaign's `sent` last moved.

### 5c. Email Validation & Cleanup (`app/services/email_validation.py`, `email_cleanup.py`)
- **Verdicts:** `invalid` (can never receive mail), `risky` (disposable domain, role account such as `support@`, probable typo of a big provider), `unknown` (a DNS lookup failed transiently), `valid`. Checks: RFC 5321/5322 syntax (non-ASCII local parts and IDN domains are accepted), domain exists (NXDOMAIN → `no_domain`), mail server exists (MX; falls back to A/AAAA per RFC 5321; a Null MX per RFC 7505 → `null_mx`; nothing → `no_mx`). `hard_bounce` is added by subscriber scans only.
- **The one rule that matters:** only a *definitive* DNS answer may produce `invalid`. A timeout, SERVFAIL, `NoNameservers` or any raw exception is `unknown`, never `invalid`: callers blocklist on `invalid`. The DNS call goes through the single seam `email_validation._resolve` (tests replace it; no network in tests). Per-domain results are cached (1 h; transient `unknown` only 60 s), bounded (50,000, swept) with in-flight dedup; `invalidate()` resets it and is called from `tests/conftest.py`. Settings: `EMAIL_VALIDATION_DNS_TIMEOUT` (5 s), `EMAIL_VALIDATION_CONCURRENCY` (50), `EMAIL_VALIDATION_FALLBACK_DNS` (`1.1.1.1,8.8.8.8`).
- **Fallback resolvers:** `_resolve` asks the system resolver first and, **only when that gives no answer at all** (SERVFAIL, timeout, socket error), the fallback servers in order (one lazily built `Resolver` each, reset by `invalidate()`). Seen on a live list: the local resolver SERVFAILed a handful of domains that public DNS resolved fine. NXDOMAIN / NoAnswer are answers and are never second-guessed, whichever resolver gave them; if every resolver fails the system resolver's error propagates, so the result is still `unknown`. `resolver_healthy()` therefore passes when any configured resolver works. **Privacy:** the fallback servers see the *domain* of an address the system resolver could not resolve; an empty setting turns the fallback off, and a malformed list is never replaced by the default (`config._env_ip_list` drops bad entries) because the list decides who sees your subscribers' domains. Tests that need the real logic use `REAL_RESOLVE` and script `_get_resolver` / `_get_fallback_resolvers`.
- **Typo detection is conservative:** an explicit misspelling table, TLD slips (`.con`), and edit-distance matching limited to dropped / doubled / swapped letters against long-named providers. **Substitutions are excluded on purpose** (`email.com` vs `gmail.com` are two real providers).
- **Not covered, deliberately:** whether a specific *mailbox* exists (needs an SMTP `RCPT TO` probe: unreliable, often blocked, can hurt the sending IP). Do not add one without an opt-in.
- **Jobs:** `scan` (report only, optionally one list) and `cleanup` (scan + **blocklist**, never delete). One job at a time (`email_cleanup._job`, claimed synchronously in `_begin_job`; `409` when busy); started in the background via `task_utils.spawn`, progress polled from `GET /api/email-validation/status`. Only `status = 'enabled'` subscribers are read. Hard-bounced subscribers come from `get_bounces(bounce_type="hard")` with the same opener exclusion as the Bounces page (`bounce_filters.filter_bounces_excluding_openers`) and are matched by `subscriber_id` or email.
- **What cleanup acts on:** `invalid` verdicts whose reasons are enabled in the settings (`clean_invalid` → syntax / no_domain / no_mx / null_mx, `clean_hard_bounces` → hard_bounce). `risky` and `unknown` are **never** acted on automatically. **A DNS-only finding (no_domain / no_mx / null_mx) needs two scans** (`_is_confirmed`): it is blocklisted only when the stored results of an earlier scan already list the subscriber as DNS-dead, because one NXDOMAIN (registrar hold, a broken zone for a day) must not cause a permanent blocklisting. Syntax errors and hard bounces are acted on at once; the waiting ones are reported as `pending_confirmation`.
- **Safety rails (cleanup only):** (1) `resolver_healthy()` must pass (a well-known domain resolves) or the run fails without touching anyone; (2) if the dead-looking addresses (confirmed or not) exceed `HOLD_FRACTION` (20 %) of the checked subscribers (judged from `HOLD_MIN_SAMPLE` = 50) the run is **held** (`status: held`): nothing is blocklisted and the user reviews on the page; (3) candidates are collected first and blocklisted afterwards in chunks of 100, never while paginating (ListMonk paginates by offset); (4) partial failures are reported (`failed_blocklist`, `status: failed`), the unblocklisted rows stay listed for a retry.
- **Schedule:** `latest_slot(cfg, now)` / `next_slot` are pure (timezone via `zoneinfo`; monthly uses days 1–28). A run is owed when `last_slot < latest_slot`. Enabling or moving the schedule **baselines** `last_slot` to the current slot, so saving "daily at 03:00" at 10:00 never fires at once; a slot missed while the app was down runs once on startup; a held / ok / cancelled run uses its slot up, a `failed` one keeps it and is retried after `RETRY_AFTER_FAILURE` (1 h).
- **State:** `email_cleanup.json` (settings, `last_slot`, `last_scan`, `last_run`, `recent_cleaned` audit trail ≤ 100) and `email_validation_results.json` (the problem addresses only, capped at 50,000, worst first when trimming; valid addresses are counted, never stored). A complete full scan replaces the results; a list scan or a cancelled one only replaces the entries of subscribers it re-checked. Both are written atomically; module locks `_STATE_LOCK` / `_RESULTS_LOCK` are reset in `tests/conftest.py` (rule 11).
- **Results actions** only touch submitted ids (rule 14): `PUT /results/blocklist` additionally refuses ids that are not in the stored results (`skipped`), and *Dismiss* (`DELETE /results`) forgets rows without touching ListMonk.
- Frontend: `Validation` (`validation.js`) paints its shell once and repaints `#valScan` / `#valResults` in place, so the quick-check box and search field keep their input while a scan reports progress; the status poll stops when leaving the page (`App.route`). Settings → *Email Cleanup* (`Settings.renderCleanup`) syncs its live parts in place (`_syncCleanupStatus`), so unsaved form edits survive; turning the switch on saves the on-screen schedule with it.

### 5a. Reply Classification (`app/services/imap_reply_tracker.py`)
- **Only replies that are part of a campaign are stored, shown, or counted.** Newsletter/list
  mail (`List-Unsubscribe`, `List-Id`, `Precedence: bulk|list|junk` without
  `In-Reply-To`/`References`) is skipped, and so is anything that cannot be attributed to a
  campaign — a ticket desk answering from its own address, a stranger's "Re: Hi", a
  subscriber's answer to a deleted or renamed campaign, a threaded "Yes. Please share." from
  an address no campaign claims. A campaign subject matches exactly, or as a substring only
  when it is at least `SUBJECT_SUBSTRING_MIN` (12) characters long (a campaign titled "Hi"
  must not claim every mail containing "hi"); the most specific match wins, not the newest.
  Attribution = the subject matches a campaign, or the sender
  is a ListMonk subscriber whose lists pick one (`match_campaign_for_reply`). A human reply
  that ends up with no campaign is dropped (`not_reply`, recorded in `reply_processed.json`);
  **when the campaign history was not fully available** (fetch failed or hit
  `MAX_CAMPAIGN_PAGES`) the message is **deferred** instead — left unprocessed so the next
  scan decides with complete data, the same rule as bounce attribution. A failed subscriber
  lookup is deferred too, never treated as "unknown sender". **Automated replies are the one
  campaign-less record in the feed:** attribution is skipped for them so they never inflate
  per-campaign charts; they are shown under the Automated tab and excluded from the reply
  rate.
- **Every record carries the mailbox that produced it** (`mailbox`: the `IMAP_USER` of the
  scan). The feed, the stats and the prune are scoped to the connected mailbox, so a `.env`
  switch to another domain's inbox starts a clean feed: the previous inbox's replies are
  neither shown nor counted. They are **kept on disk** (switching back restores them); the
  prune only deletes unstamped records written before the field existed.
  The same campaign rule is applied to already-stored records (`prune_non_campaign_replies`,
  `pruned` in scan stats, before re-classification so an automated reply that loses its
  attribution there is not mistaken for a stray): a record of the connected mailbox (or an
  unstamped one) is deleted unless it has a campaign, is `auto_reply`, or was acted on by a human
  (a `manual` status_source, or replied/archived — triage work is never destroyed, the read
  filter simply hides it). The prune needs no ListMonk lookups: the attribution on the
  record is the only evidence that counts.
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
- **Locales** (`app/services/reply_i18n.py`): German, French, Spanish, Italian, Portuguese,
  Dutch, Polish and Danish/Norwegian/Swedish wording is appended to the same-named English
  lists (`_NEGATIVE_PATTERNS`, `_ACK_PATTERNS` …) at import, so one classifier and one
  ordering cover every language — a Polish "nie jestem zainteresowany" is `negative`, not
  `neutral`. Spelling is accent-tolerant. **Precision rules, because a wrong `auto_reply`
  tag can blocklist a real prospect when the auto-blocklist toggles are on:** acknowledgement
  patterns match *receipt / promise-to-answer* phrasing only (never a bare "thanks for your
  message"); dead-address phrases must sit next to a mailbox word ("no longer available" can
  describe anything); and the human-signal override is deliberately narrow — explicit
  interest / refusal only, never "call me" / "senden Sie mir" (vacation notices say those) or
  "désinscrire" (footers do). Third-person promotional offers ("Wenn Sie interessiert sind …",
  "Dear Interested Party") are filtered in every locale (`CONDITIONAL_OFFER_RE`).
  B2B opt-outs are written for the company, so "remove **us**" / "take **us** off" are
  refusals alongside the singular forms. New phrasing goes in `reply_i18n.py` with a case in
  `tests/test_real_world_replies.py` — including a look-alike a person could write.
- Re-classification re-strips stored bodies with the current rules and restores campaign
  attribution (`reattribute_replies`) for records that stop being `auto_reply`. An
  `auto_reply` never carries a campaign: the clear also runs when the verdict is unchanged
  (records stored before attribution was skipped for automated mail inflated the
  per-campaign chart).
- **Header backfill**: records stored before automation-header support have no `auto_header`
  and no `headers_checked`. The scan re-fetches their message once (inside the normal scan
  window) to read `Auto-Submitted` / `X-Autoreply`, stores the verdict, then re-classifies —
  a responder with no tell-tale wording (e.g. a registration auto-reply) was otherwise a
  neutral reply, or a fake Hot Lead, forever. New records are written with
  `headers_checked: true`. Records whose message is older than the scan window (or beyond
  its 200-message cap) are looked up by `SEARCH HEADER Message-ID` instead
  (`lookup_header_verdicts`, at most `HEADER_LOOKUP_LIMIT` per scan, headers only via
  `BODY.PEEK[HEADER]`, exact-id match because IMAP matches substrings). The verdict is final
  whether the headers say nothing or the message has left the INBOX; an IMAP error is *not*
  final and is retried. Message-IDs come from strangers, so only ids made of RFC 5322
  characters (no quote / backslash / CRLF) are ever put into a search command.
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
- `app/config.py` provides `settings.data_path(filename)` to store mutable JSON state files (`schedule.json`, `warmup.json`, `send_stats.json`, `auto_unblock.json`, `email_cleanup.json`, `email_validation_results.json`, `app_settings.json`, `unsubscribe_log.json`, `unsubscribe_settings.json`, `unsubscribe_processed.json`, `unsubscribe_examined.json`, `reply_log.json`, `reply_processed.json`, `reply_settings.json`, `bounce_processed.json`, `session_revocations.json`, `manual_blocklist.json`).
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
| | `/api/subscribers/{id}/unblock` | PUT | Re-enable a blocklisted subscriber (echoes email/name/attribs/lists — ListMonk's PUT is a full replace; `409` if not blocklisted) and `blocklist_registry.unmark`s it |
| | `/api/subscribers/blocklist` | PUT | Bulk blocklist subscribers by ID array |
| | `/api/subscribers/lists` | PUT | Bulk modify list memberships (add/remove/unsub) |
| | `/api/subscribers` | POST / DELETE | Create subscriber / bulk delete subscribers by ID array |
| **Lists** | `/api/lists` | GET / POST | List all mailing lists / create new list |
| | `/api/lists` | DELETE | Bulk delete lists by ID array + delete their non-blocklisted subscribers and dismiss their Email Validation results (blocklisted subscribers stay on the blocklist) |
| | `/api/lists/{id}` | GET / PUT / DELETE | Get / update / delete list (DELETE also removes non-blocklisted subscribers of the list and dismisses their Email Validation results, keeping blocklisted subscribers on the blocklist) |
| **Campaigns** | `/api/campaigns` | GET / POST | List campaigns (with hard bounce cache) / create |
| | `/api/campaigns/running/stats` | GET | Real-time running campaign stats |
| | `/api/campaigns/summary` | GET | Aggregate totals across all campaigns (cached 60s; `fresh=true` — the dashboard's Refresh — skips the cache) |
| | `/api/campaigns/export-all` | GET | Export all campaigns summary as CSV |
| | `/api/campaigns/analytics/{type}` | GET | Campaign analytics (views/clicks/bounces/links — the type is whitelisted before it reaches the upstream URL) |
| | `/api/campaigns/analytics/{type}/export` | GET | Export campaign analytics as CSV |
| | `/api/campaigns/{id}` | GET / PUT / DELETE | Get / update / delete campaign |
| | `/api/campaigns/{id}/preview` | GET | Preview campaign HTML rendering |
| | `/api/campaigns/{id}/preview` | POST | Render unsaved edits (`body`, `content_type`, `template_id`) with ListMonk; sandboxed HTML (the editor's Preview) |
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
| | `/api/templates/preview` | POST | Render an unsaved template `body` with ListMonk's sample data (the editor's live preview; registered before `/{id}`) |
| | `/api/templates/{id}/preview` | GET | Template rendered by ListMonk with its sample campaign/subscriber (sandboxed HTML); the UI falls back to the raw body when rendering fails |
| **Bounces** | `/api/bounces` | GET / DELETE | List bounces (filtered) / bulk delete (`ids` narrows to a selection, `campaign_id`+`bounce_type` widen to a sweep) |
| | `/api/bounces/ingest` | POST | Trigger bounce mailbox IMAP ingestion |
| | `/api/bounces/export` | GET | Export filtered bounce records as CSV |
| | `/api/bounces/{id}` | DELETE | Delete single bounce record |
| **CSV Converter** | `/api/converter/detect-columns` | POST | Detect CSV headers & preview rows |
| | `/api/converter/preview` | POST | Convert and return the first rows as JSON (exact ListMonk output preview) |
| | `/api/converter/convert` | POST | Convert CSV to ListMonk schema (download) |
| | `/api/converter/convert-and-import` | POST | Convert and import directly into ListMonk (`subscription_status` defaults to `confirmed` so double-opt-in lists are mailable; `overwrite_userinfo` / `overwrite_subscription_status` map onto ListMonk's granular upsert flags and default to true / **false** (overwriting statuses re-subscribes opted-out contacts, so it must be asked for); rejects `no_attributes` mappings and empty conversions; `validate_emails=true` runs the address validator first and leaves out rows whose verdict is `invalid` only, reported as `validation`) |
| **Scheduler** | `/api/scheduler` | GET / PUT | Get / update send window settings |
| | `/api/scheduler/run` | POST | Manually trigger scheduler tick |
| **Warm-up** | `/api/warmup` | GET / PUT | Ramp settings + today's quota, usage, forecast and recent days / change settings (strictly typed, unknown keys → 400, `target_volume < start_volume` → 400). `level` jumps the ramp to a given day. Ticks the scheduler right away |
| | `/api/warmup/preview` | GET | The ramp a `start_volume` / `growth_percent` / `target_volume` triple would produce (the settings form's live chart) |
| | `/api/warmup/insights` | GET | Queued recipients (running + paused campaigns), days to send them on this ramp, ListMonk's max messages/second and an overshoot warning. Best effort: parts that cannot be read from ListMonk are `null`, never a 5xx |
| **Send Stats** | `/api/sends/daily` | GET | `days` (1–365): emails sent per calendar day (oldest first, `tracked` flag), `sent_today`, `today_by_campaign` (each with `name` and `deleted`: sends by a since-deleted campaign still count), `sent_today_by_deleted`, `tracking_since`, `timezone` |
| | `/api/sends/progress` | GET | Running campaigns' live `sent`/`to_send`, `idle_seconds` since the count last moved and `stalled` (flat for over 2 sliding windows + 5 min, or 15 min without a window; 2 h if ListMonk's settings are unreadable), plus `last_poll` (the dashboard's "send counters checked … ago") |
| **Auto-Unblock** | `/api/auto-unblock/status` | GET | Count of blocklisted engaged contacts plus `enabled`, `last_run`, `history` (latest 50) |
| | `/api/auto-unblock/settings` | PUT | `{"enabled": bool}` only: turns the six-hourly background run on/off ("Run now" always works) |
| | `/api/auto-unblock/run` | POST | Manually run auto-unblock process |
| **Email Validation** | `/api/email-validation/check` | POST | `{"emails": [...≤100]}`: verdict, reasons, detail and typo suggestion per address. Never touches ListMonk |
| | `/api/email-validation/status` | GET | Running job progress (`running`, `kind`, `phase`, `checked`/`total`, counts) + the latest scan summary + stored-problem counts |
| | `/api/email-validation/scan` | POST | Start a report-only scan in the background; optional `{"list_id": n}` (positive int, else 400); `409` while a job runs |
| | `/api/email-validation/scan/cancel` | POST | Stop the running job after the current page (`409` when nothing runs) |
| | `/api/email-validation/results` | GET | Stored problem addresses, worst first; `verdict` (`invalid`/`risky`/`unknown`), `search` (in-memory substring, never ListMonk SQL), `page` (≤ 100,000), `per_page` (≤ 200) |
| | `/api/email-validation/results/export` | GET | CSV of the stored problems (formula-injection safe); `404` when empty |
| | `/api/email-validation/results/blocklist` | PUT | `{"ids": [...]}` blocklist the selected rows only (`parse_ids`, chunks of 100). Ids not in the stored results are `skipped`; failures are listed per id |
| | `/api/email-validation/results` | DELETE | `?ids=…` dismiss rows from the list; ListMonk is not touched |
| | `/api/email-validation/cleanup` | GET / PUT | Cleanup settings + `next_run`, last scan / run, recent audit trail. PUT is strictly typed (unknown key, bool-as-int, bad timezone / time / day → 400; enabling with nothing to clean → 400) and baselines the schedule slot |
| | `/api/email-validation/cleanup/run` | POST | Run the real cleanup now in the background (`409` while a job runs, `400` when no criterion is enabled) |
| **Unsubscribes** | `/api/unsubscribes` | GET | Paginated unsubscribe log |
| | `/api/unsubscribes/stats` | GET | Unsubscribe counts & source breakdown |
| | `/api/unsubscribes/settings` | GET / PUT | Get / update unsubscribe settings (blocklist toggle) |
| | `/api/unsubscribes/campaigns` | GET | Unsubscribes grouped by campaign |
| | `/api/unsubscribes/campaigns` | DELETE | Bulk delete the records of several selected campaigns by ID array |
| | `/api/unsubscribes/campaign/{id}` | GET / DELETE | Get / delete records for specific campaign; `id` may be `unknown` for the records no campaign could be attributed to (also accepted by `/export` and as `campaign_id` of `DELETE /records`) |
| | `/api/unsubscribes/campaign/{id}/export` | GET | Export campaign unsubscribes as CSV |
| | `/api/unsubscribes/records` | DELETE | Delete specific records by email |
| | `/api/unsubscribes/export` | GET | Export all unsubscribes as CSV |
| | `/api/unsubscribes/scan` | POST | Manually trigger IMAP + link scan (502 when a configured mailbox is unreachable; 200 with an explicit "not configured — link scan only" message otherwise) |
| | `/api/unsubscribes/imap-status` | GET | Check IMAP connection health |
| | `/api/unsubscribes/clear` | DELETE | Clear all unsubscribe log records |
| | `/api/unsubscribes/reset` | POST | Undo all unsubscribes & restore list memberships |
| **Replies** | `/api/replies` | GET | Paginated prospect replies with sentiment/status filtering (campaign-attributed replies plus automated ones only — an unattributed reply is never shown) |
| | `/api/replies/timeline` | GET | Human, campaign-attributed replies per calendar day (`campaign_id`, `from_date`/`to_date` inclusive, `tz` day boundary) for the Analytics "Replies over time" chart |
| | `/api/replies/stats` | GET | Aggregate reply counts, reply rate %, and sentiment breakdown over the same visible set (optional `campaign_id` scopes counts and rate to one campaign; a `by_campaign` breakdown is always returned for the analytics chart; the reply rate counts only campaign-bound human replies) |
| | `/api/replies/scan` | POST | Manually trigger IMAP prospect reply scan (502 when the mailbox scan fails — never a 200 "ok" with a buried error) |
| | `/api/replies/status` | GET | Check IMAP connection status |
| | `/api/replies/settings` | GET / PUT | Get / update reply tracker settings: `auto_tag_leads` (positive → Hot Lead), `scan_interval_minutes` (1–1440), `auto_blocklist_machine`, `auto_blocklist_ooo` |
| | `/api/replies/status` | PUT | Bulk update reply status from the feed's selection bar |
| | `/api/replies/blocklist` | POST | Bulk blocklist every selected reply's sender |
| | `/api/replies` | DELETE | Bulk delete replies by ID array |
| | `/api/replies/{id}/status` | PUT | Update reply status (unread, lead, replied, archived) |
| | `/api/replies/{id}/blocklist` | POST | Blocklist the ListMonk subscriber behind a reply (resolved server-side) |
| | `/api/replies/{id}` | DELETE | Delete / dismiss single reply record |
| **Deliverability** | `/api/deliverability/reputation` | GET | Sending reputation report (`report`), its audit rows (`checks`) and the dashboard `alerts`; cached 15 min, `refresh=true` re-checks |
| | `/api/deliverability/checklist` | GET | (`refresh=true` re-runs the reputation DNS checks — the page's Refresh) Audit Listmonk settings against deliverability rules (10 checks: tracking, individual tracking, unsubscribe header, batch size, concurrency, public archive, bounce feedback loop, hard-bounce action, send cadence, upload allow-list). The one-click `List-Unsubscribe` header is **required** (Gmail/Yahoo bulk-sender rules) and both presets keep it on |
| | `/api/deliverability/apply-preset` | POST | 1-click apply 'cold_email' or 'newsletter' preset (merged onto current settings via `apply_settings`) |
| | `/api/deliverability/settings` | PUT | Change individual settings from the page's row controls. Only keys in `deliverability_service.EDITABLE_SETTINGS` are accepted (strictly typed, no coercion → 400); the virtual `bounce.actions.hard` key is merged into `bounce.actions`. Returns the refreshed checklist (retried briefly while ListMonk reloads) |
| | `/api/deliverability/email-format` | GET | Draft/scheduled/paused/running campaigns with their format (`html`, `plain`, or locked `text`/`visual`), template and why a switch is blocked (running → "pause first") |
| | `/api/deliverability/email-format/{id}` | PUT | `{"format": "html"\|"plain"}` — swap the campaign's `template_id` + `altbody`; see *Email format switch* below |
| | `/api/deliverability/unsubscribe-link` | PUT | `{"enabled": bool, "template_ids"?: [...]}` — show/hide the **visible footer** unsubscribe link in campaign templates. `privacy.unsubscribe_header` only sets the List-Unsubscribe header and never changes the body. Hiding wraps the link in a never-rendered `{{/* lmp:unsub-off */}}{{ if false }}…{{ end }}` block (a paragraph holding only unsubscribe links is hidden whole); enabling strips the wrapper byte-for-byte. Transactional templates and templates with no link are never edited; partial failures are reported, total failure is a 502 |

**Email format switch** (`app/services/email_format.py`): a template is a *design* when it
contains `<img>`, `<table>` or `<style>`, otherwise *plain*. Switching only changes the
campaign's `template_id` and `altbody` (both formats stay `richtext`). Each format's
`{template_id, altbody}` is remembered in the campaign's own `attribs.lmp_email_format`
(stored in ListMonk, so every app instance sees it); `PUT /api/campaigns/{id}` therefore
carries the stored `attribs` over when the client omits them, because ListMonk writes
`attribs` unconditionally. Without memory, the plain version is an app-managed twin
(`"<name> · plain"`, marked `{{/* lmp:plain-of=<id> */}}`, regenerated from the design on
each such switch) and the text part is derived from the design: output actions are wrapped
in `Safe` (the altbody is an html/template), `{{ template "content" . }}` is replaced by a
text snapshot of the campaign body (it would recurse inside the altbody), and ListMonk's link
helpers (`UnsubscribeURL`, `TrackLink`, …) are left in their bare shorthand, the only form
ListMonk's pre-compile rewrite resolves. ListMonk compiles templates only at send time, so
every switch renders the campaign preview and rolls back on a compile error (`422`).

The audit has 21 checks: the ten above plus `unsubscribe_link` (footer link state read from
the campaign templates; `info` when templates are unreadable or none has a link),
`public_subscription_page`, `sliding_window_rate`, `hidden_text`, and the seven
**Sending reputation** rows (group `reputation`, `sending_reputation.build_checks`):
`reverse_dns`, `ptr_alignment`, `ip_blocklists`, `spf`, `dkim`, `dmarc`, `sender_rejections`.
The templates are fetched once per audit (`template_unsubscribe.campaign_templates`).

**Sending reputation** (`app/services/sending_reputation.py`): the audit used to score 100 %
next to an IP whose PTR named another domain and an IP on Barracuda. Rules:
- Sending IPs = `SENDING_IPS`, else the public A records of ListMonk's *enabled* SMTP hosts. A
  known ESP host (SES, SendGrid … `ESP_HOST_SUFFIXES`) makes the IP rows `info` ("managed by").
- A DNSBL counts only if it answers its RFC 5782 test entry (`127.0.0.2`); a `127.255.255.x`
  answer is the operator refusing (Spamhaus vs public resolvers) → `unknown`, **never clean**.
- SPF is evaluated for the IP (ip4/ip6/a/mx/include/redirect/exists/all, 10-lookup limit,
  macros skipped, `ptr` ignored). DKIM: `DKIM_SELECTORS` are tried. DMARC falls back to the
  organisational domain (last two labels).
- `sender_rejections` reads soft bounces of the last 7 days whose `meta.classify_reason` is a
  sender tag (`reputation_policy`, `blacklist`, `reverse_dns`, `auth_failed`; see
  `bounce_ingest.classify_bounce`, which matches receiver error wording only — never bare
  `dkim`/`spf`, because a DSN returns the original headers). Blocklist / rDNS / auth → fail;
  spam-policy only → warn, fail at ≥ 5 % of the period's sends (min 3).
- DNS goes through the single seam `sending_reputation._query` (on top of
  `email_validation._resolve`); `tests/conftest.py` makes it time out for every test and
  calls `invalidate()` (15-min report cache). Rows carry `chips` / `links` that the
  Deliverability page renders generically.

**ListMonk's sending engine, as it really behaves (v6, `internal/manager/pipe.go`)** — the
audit and the cold preset are built on these facts, verified on a live incident:
- Every campaign is drained through **one** loop, and the sliding-window sleep happens
  *inside* a fetched batch. Pausing, cancelling or deleting a campaign does not end that
  loop: it keeps pushing (and discarding) the rest of its batch, one window at a time, and
  every other campaign waits behind it with no error. 100 messages at 1 per 15 min held a
  freshly started campaign at 0 sent for hours. Hence `batch_size` must be ≤ the window
  rate when a window is on (`batch_size` check: hold time in the description; the cold
  preset sets `batch = window rate`), and `/api/sends/progress` + the dashboard flag a
  running campaign whose count stays flat.
- `sending_window()`: with a window rate of **1**, two messages leave per window (the limit
  is tested with the elapsed time measured before the expired window is reset). Use 2 per
  double the duration. `send_cadence_per_day` uses this effective rate.
- `last_subscriber_id` advances to the end of a batch when it is *fetched*; only a clean
  stop writes back the last subscriber really sent. A ListMonk restart (or a settings
  reload) mid-batch therefore skips the unsent rest of that batch — another reason for
  small batches.

`hidden_text` fails on any element hidden by inline style (display:none, visibility:hidden,
0 font-size / opacity / max-height) whose text, without markup and template actions, is over
`HIDDEN_TEXT_LIMIT` (200) characters: invisible filler is a spam-filter evasion pattern; a
preheader is short and passes.

**Mode-aware:** once a mode has been chosen (`app_settings.mode_selected`), Newsletter mode
reports the cold-outreach-only checks (`COLD_ONLY_CHECKS`: tracking, individual tracking,
batch size, concurrency) as `info`, so the app's own newsletter preset does not fail the
app's own audit. The newsletter preset no longer switches the public archive / subscription
page on.

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
| Leads/Subscribers | View details, Edit (incl. list memberships), Blocklist / Unblock, Delete | Add/Remove from list, Blocklist, Export Selected (CSV), Delete |
| Lead Lists | View Subscribers (jumps to the filtered Lead table), Export, Edit, Delete | Export Subscribers, Delete |
| Cold Outreach/Campaigns | View, Start/Pause/Resume, Test Send, Archive, Delete | Archive, Export Selected (CSV), Delete |
| Templates | Preview, Duplicate, Edit, Set Default, Delete (search by name) | Delete |
| Bounces | Delete | Export Selected (CSV), Delete Selected (plus the existing filter-wide Delete All) |
| Replies & Leads | Reading pane: status switch (Unread / Hot lead / Replied / Archived), Copy Email, Blocklist, Delete (`J`/`K` or ↑/↓ move between replies) | Mark Hot Lead / Replied / Archived / Unread, Blocklist, Delete |
| Unsubscribes | per-campaign Remove, Remove List (detail view), record removal | Delete Records for the selected campaign cards |
| Email Validation | Blocklist, Dismiss | Blocklist Selected, Dismiss Selected, Export Selected (CSV); filter-wide Export CSV |

Frontend conventions:

- Shared helpers in `app.js`: `App.term('subscriber'|'list', {plural, cap})` gives the
  mode-aware noun (Leads / Lead Lists in Cold Email mode; page titles use `coldTitle`);
  `App.moreMenu(items)` renders the "⋯" overflow menu (secondary and destructive row /
  page actions go there; its popover moves to `<body>` while open because glass cards'
  `backdrop-filter` breaks `position: fixed`); `App.showForm(title, html, label, fn)` is
  the dialog for small forms (Cancel + one primary button, never `showResult`'s extra OK);
  `App.emptyChart()` replaces a chart that has nothing to draw; `App.autoRefresh(page, fn, ms)`
  keeps a page live while a campaign sends (cleared by `route()`, skipped while typing,
  a dialog or a menu is open); `App.debounce` drives search-as-you-type.
- Automation that acts on people without a click lives in Settings (tabs: Send Window,
  Warm-up, Automation, Email Cleanup, Auto-Unblock; deep link `#/settings?tab=…`). The
  Unsubscribes and Replies pages link there instead of carrying their own toggles.

- A page calls `App.selection.register(key, {noun, pageValues, actions})` on every
  render, places `App.selection.bar(key)` above its table and wires
  `${App.selection.headbox(key, label)}` / `${App.selection.rowbox(key, value, label)}`
  into the markup. Values are held as strings so a selection survives pagination.
- Handlers read `App.selection.values(key)`, act once, then `App.selection.clear(key)`
  and re-render. Selection state is per page and dropped on navigation
  (`App.selection.clearAll()` in `App.route`).
- Client-side CSVs (`Export Selected`) only cover rows loaded on the current page;
  the filter-wide exports still use the streaming server endpoints.
- The bulk bar pluralises `noun` with a plain "s"; a noun that does not ("reply" →
  "replies") passes `plural` to `App.selection.register`.

Replies page (`static/js/replies.js`) is an inbox, not a feed of cards:

- `render()` is the navigation entry and reads the deep-link query **once**;
  every in-page change (filter, search, status, scan, bulk action) goes through
  `reload()` → `refresh()`, which repaints only the data-dependent regions
  (`#repliesHero`, `#repliesFilters`, `#repliesAuto`, `#repliesBody`, `#repliesPager`).
  The search box and scroll position are therefore never rebuilt. Do not call
  `render()` for an in-page change — a stale route query would override the filter
  that was just chosen.
- The sentiment tabs in the summary strip *are* the sentiment filter; the
  auto-blocklist / auto-flag / scan-interval settings live in a collapsible
  "Automation" panel. Sentiment colour is a single `--sent` custom property set by
  `.sent-*` classes, so rows, tabs, badges and the distribution bar stay in sync.
- The reading pane is `position: sticky` beside the list; below 900px it replaces
  the list (`is-reading` on `#repliesPage` / `#repliesLayout`) and the page hides
  its summary and toolbar so the reply opens at the top of the screen.
- Everything interpolated into markup goes through `Replies._esc()` /
  `Replies._arg()` (the latter for values inside `onclick` attributes).

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
3. **Escaping ListMonk SQL queries** (only `/api/subscribers` takes SQL: the `query` of `/api/lists` and `/api/campaigns` is a **plain-text** search, and a SQL fragment there makes ListMonk answer 500, so those go through `search_query.plain_search_term`) — use `safe_email_for_query` from `imap_helpers.py` when passing user input to ListMonk queries. ListMonk's subscriber `query` parameter is raw SQL: free-text search boxes must go through a builder like `subscribers._search_to_query` (strips LIKE wildcards/backslashes, doubles quotes, wraps in ILIKE), never through `query` verbatim. Any path segment interpolated into an upstream ListMonk URL (e.g. `analytics_type`) must be whitelisted first.
4. **Memory efficiency** — for CSV exports, prefer `aiter_dicts_to_csv` from `export_service.py` fed by `listmonk.iter_pages(...)` so rows stream one page at a time. `dict_list_to_csv` still buffers its input list, so only use it when the dataset is already materialised (e.g. after in-process filtering). Both writers neutralize spreadsheet formula injection (`=`, `+`, `-`, `@`, tab, CR leading characters get a `'` prefix) — client-side "Export Selected" builders must use `App.csvCell()` for the same reason.
5. **Bound pagination inputs** — every endpoint taking `page`/`per_page` must declare `Query(..., ge=1, le=MAX)`. Bounce listing is the expensive one: serving page N scans every preceding record, so an unbounded `per_page` multiplies into tens of thousands of ListMonk queries.
6. **Never chain `.get(k, {}).get(...)`** on ListMonk response fields that can be explicitly null (notably `bounce["campaign"]`). Use the helpers in `bounce_filters.py` (`bounce_campaign_id` / `bounce_campaign_name`).
7. **Failed actions return a status code** — raise `HTTPException` rather than returning `200 {"error": ...}`, so monitoring and the frontend can distinguish success from failure. The one exception is `/api/auto-unblock/status`, which degrades to a `200` with an `error` field because the Settings page loads it via `Promise.all`.
8. **Thread/Async Safety** — always use `asyncio.Lock` when modifying shared mutable resources (`_INGEST_LOCK`, `_scan_lock`, `_log_lock`).
9. **Persistent Storage** — respect `DATA_DIR` for mounted volumes and file storage (`session_secret.key`, `reply_log.json`, etc.).
10. **Frontend error contract** — `API.request` toasts server errors once and marks the rejection `err.handled = true`. Callers must not toast again; wrap mutating handlers in `try/catch` or the promise rejection is unhandled.
11. **Module-level caches** — any service cache in a module global needs an invalidation hook and must be reset by `tests/conftest.py`, or tests leak state into each other.
12. **ListMonk settings are all-or-nothing** (masked secrets are blanked at every nesting depth, e.g. `security.captcha.hcaptcha.secret`) — `PUT /api/settings` validates the whole payload (a partial body fails with *"At least one SMTP block should be enabled"*), and the GET response returns every secret masked with `•`. Never PUT a partial dict and never echo a mask back: use `listmonk.apply_settings(patch)`, which merges onto the current settings and blanks masked secrets so ListMonk keeps the stored value (matched by SMTP/bounce UUID).
13. **Never treat IMAP `\Seen` as "already processed"** — the scanners work on mailboxes a human reads. Bounce ingestion persists handled DSN Message-IDs to `bounce_processed.json`; the unsubscribe and reply scanners do the same. New scanners must follow that pattern (or keep an equivalent state file).
14. **Bulk actions only touch the submitted IDs** — never widen a selection into a filter-wide sweep. Validate IDs with `bulk_ops.parse_ids`, run per-item calls through `bulk_ops.run_bulk`, report partial failures (`errors`/`failed`) in the response, and register static bulk routes (`/archive`) before parameterized ones (`/{id}`).