# ListMonk Pro — Comprehensive Code Review Findings

**Date:** 2026-10-19  
**Scope:** Full project review — every page, every feature, every file  
**Test Suite Status:** ✅ All 430 tests passing  
**Review Method:** 6 parallel deep-dive agents covering routers, services, frontend JS, auth/config/infra, HTML/CSS, and test coverage

---

## Executive Summary

The codebase is **well-architected and mature**, with strong attention to security (SQL injection prevention, formula injection sanitization, session signing), idempotency (bounce/reply/unsubscribe processed-ID sets), and operational safety (atomic writes, bounded concurrency, capped state files). The test suite is extensive at 430 tests.

However, the review identified **~155 issues** across all layers, including **6 critical**, **~28 high**, **~55 medium**, and **~66 low** severity findings. The most urgent are race conditions in reply log mutations, missing CSRF protection, and accessibility blockers.

---

## Critical Issues (Fix Immediately)

### C1. Race Conditions in `reply_log.py` — Silent Data Loss
- **File:** `app/services/reply_log.py`, lines 77–131
- **Impact:** `update_reply_status()`, `update_reply_statuses()`, `delete_replies()`, and `mark_processed_message_ids()` all perform read→modify→write on `reply_log.json` / `reply_processed.json` **without acquiring any lock**. If a background scan's `append_log` runs between the load and save, new records are silently dropped.
- **Fix:** Wrap each function in `async with _log_lock:` (make them async) or use a dedicated lock for the processed file.

### C2. Missing `app/__init__.py`
- **File:** `app/__init__.py` (does not exist)
- **Impact:** Listed in AGENTS.md project structure but missing from disk. Can cause `ModuleNotFoundError` in certain deployment scenarios, CI runners, or when pytest collects tests differently.
- **Fix:** Create an empty `app/__init__.py`.

### C3. No CSRF Protection on State-Changing API Endpoints
- **File:** `app/main.py` (all `/api/*` POST/PUT/DELETE routes)
- **Impact:** All mutating endpoints rely solely on `SameSite=Lax` cookies. Same-origin attacks (XSS in templates, compromised static assets) can make authenticated API calls. Logout was correctly made POST-only, but no other endpoint has CSRF tokens.
- **Fix:** Implement double-submit cookie pattern or require `X-Requested-With: XMLHttpRequest` header.

### C4. Rate Limiter Is In-Memory Only — Resets on Restart
- **File:** `app/auth.py`, lines 32–60
- **Impact:** `_login_failures` dict clears on every restart. An attacker who triggers container restarts resets the limiter entirely, enabling indefinite brute-force.
- **Fix:** Persist rate-limit state to `DATA_DIR`, or log a startup warning that state was lost.

### C5. Hardcoded Default Credentials in `.env.example`
- **File:** `.env.example`, lines 10–11
- **Impact:** `admin/changeme` defaults ship if users copy `.env.example` without editing. No enforcement mechanism exists.
- **Fix:** Refuse to start when credentials match known defaults, or remove defaults entirely.

### C6. Non-Atomic Counter Mutations in `auto_unblock.py` Under Concurrency
- **File:** `app/services/auto_unblock.py`, lines 65, 89, 117–119
- **Impact:** `deleted += 1`, `success += 1`, `unblocked.append(...)` mutated from concurrent tasks inside `asyncio.gather` without synchronization. Can produce incorrect counts and missed unblocks.
- **Fix:** Collect results from gather and aggregate after, or use an asyncio.Lock.

---

## High Severity Issues

### H1. X-Forwarded-For Parsing Direction May Be Wrong for Multi-Hop Chains
- **File:** `app/auth.py`, lines 96–105
- Uses `entries[-1]` (rightmost). Correct for single-proxy nginx setups but breaks for multi-hop chains. Document the assumption or implement configurable `TRUSTED_PROXY_COUNT`.

### H2. Session Cookie Lacks `Secure` Flag When Proxy Misconfigured
- **File:** `app/auth.py`, lines 168–180
- If reverse proxy doesn't set `X-Forwarded-Proto`, cookies are sent without `Secure` flag over plain HTTP. Add startup warning when `TRUST_PROXY=true` but no XFP header seen.

### H3. No Content-Security-Policy Header
- **File:** `app/main.py`, lines 85–99
- No CSP header or meta tag. Any future XSS vulnerability has no mitigation layer. Add baseline: `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'`.

### H4. Modal Focus Traps Missing
- **File:** `templates/index.html`, lines 151–197, 200–212
- Tab key cycles through background content while modals are open. Violates WCAG 2.4.3. Implement focus trap in JS.

### H5. Onboarding Mode Cards Not Keyboard Accessible
- **File:** `templates/index.html`, lines 160, 178
- `.mode-choice-card` divs use `onclick` but lack `tabindex`, keyboard handlers, and ARIA roles. Keyboard-only users cannot select a mode on first run.

### H6. Inline Event Handlers Throughout Templates
- **Files:** `templates/index.html` (lines 122, 126, 135, 160, 178), `templates/login.html` (lines 442–449, 505–550)
- Multiple `onclick` attributes and inline `<script>` blocks violate CSP and increase XSS surface. Replace with `addEventListener` + `data-*` attributes.

### H7. TOCTOU Lock-Check Pattern in 4 Scanners
- **Files:** `bounce_ingest.py:475`, `imap_unsubscribe.py:428`, `link_unsubscribe.py:206`, `imap_reply_tracker.py:647`
- `if _LOCK.locked(): return` followed by `async with _LOCK:` is a TOCTOU race. The fast-return becomes unreliable. Use a separate boolean flag or `acquire(timeout=0)`.

### H8. Naive Datetime in `hard_bounce_cache.py`
- **File:** `app/services/hard_bounce_cache.py`, line 69
- `datetime.now().isoformat()` uses naive datetime. Every other timestamp uses `datetime.now(timezone.utc)`. Change to UTC.

### H9. `create_bounce` Bypasses `_request` Error Handling
- **File:** `app/services/listmonk_client.py`, lines 364–385
- Calls `self.client.post()` directly instead of `self._request()`. Non-JSON 2xx responses produce fake `{"ok": True}`. Use `_request` or handle explicitly.

### H10. Side-Effect Write Inside `unsubscribe_log.load_log`
- **File:** `app/services/unsubscribe_log.py`, lines 84–86
- Migration logic calls `save_log()` as a side effect of reading. Two simultaneous reads could both migrate and overwrite each other. Separate migration into explicit one-time step.

### H11. Login Page CSS Token Mismatch
- **File:** `templates/login.html` line 18 vs `static/css/style.css` line 9
- `--primary-soft` is `#665efd` in login but `#8b85ff` in main app. Visual inconsistency shipped to users.

### H12. Campaign Analytics Date Parameters Not Validated
- **File:** `app/routers/campaigns.py`, lines 132–142, 145–166
- `get_campaign_analytics` and `export_campaign_analytics` accept `from_date`/`to_date` but don't call `_validate_date`. Other endpoints in the same file do validate. Add validation.

### H13. Reply Campaign ID Type Comparison Bug
- **File:** `app/routers/replies.py`, line 191
- `r.get("campaign_id") == campaign_id` compares potentially string (from JSON) to int (from query param). Other places in the same file use `str()` comparison. Fix to `str(r.get("campaign_id")) == str(campaign_id)`.

### H14. `unsubscribes.js` Uses Raw `fetch` Bypassing API Error Contract
- **File:** `static/js/unsubscribes.js`, lines 539–576
- `triggerScan` uses raw `fetch()` with manual 401 handling instead of `API.request`. Bypasses centralized error handling, toast deduplication, and content-type parsing. If auth middleware changes, this code won't benefit.
- **Fix:** Replace with `API.post('/api/unsubscribes/scan')` wrapped in try/catch.

### H15. No Request Timeout in `API.request`
- **File:** `static/js/api.js`, lines 13–58
- No timeout or AbortController. A hung server leaves the UI in "Loading…" forever with no way to cancel. Only the unsubscribes scan works around this with its own AbortController.
- **Fix:** Add an internal AbortController with configurable timeout (e.g., 30s for normal requests, longer for exports).

---

## Medium Severity Issues

### Security
| # | File | Issue |
|---|------|-------|
| M1 | campaigns.py, lists.py, subscribers.py | Raw `query` SQL passthrough — any authenticated user can execute arbitrary Postgres expressions |
| M2 | `app/config.py:75-92` | `data_path()` doesn't validate resolved path stays within `DATA_DIR` — path traversal if future caller passes user input |
| M3 | `app/auth.py:145-157` | Session secret fallback write to `.env` is non-atomic; duplicate lines ignored by python-dotenv |
| M4 | `Dockerfile:8-13` | `COPY . .` copies everything before switching user; secrets baked into image if `.env` not dockerignored |
| M5 | `app/main.py:85-99` | No `Permissions-Policy` header |
| M6 | `export_service.py:13-22` | Formula injection sanitizer doesn't strip leading whitespace; `" =SUM(A1)"` bypasses it |

### Bugs & Logic
| # | File | Issue |
|---|------|-------|
| M7 | campaigns.py, lists.py, subscribers.py, templates.py | All CRUD create/update endpoints accept `data: dict` with zero validation |
| M8 | `deliverability_service.py:76` | Dot-notation key access (`raw_settings.get("privacy.disable_tracking")`) — actually correct for ListMonk's flat API format (false positive on deeper inspection) |
| M9 | `unsubscribe_log.py:84-86` | Migration side-effect in load_log (see H10) |
| M10 | `replies.py:329-358` | `_blocklist_sender` raises HTTPException inside `run_bulk`, losing distinction between client errors and server errors |
| M11 | `unsubscribes.py:303-420` | Reset-all race: new unsubscribe record for restored email could be incorrectly removed |
| M12 | `bounces.py:116-187` | Delete-all with no filters wipes every bounce with no confirmation mechanism |
| M13 | `unsubscribes.py:181-190` | Email-based deletion with no cap on email count and no format validation |
| M14 | `converter.py:212-269` | Convert-and-import reads entire file into memory (~100MB peak for 50MB upload) with no timeout |

### Performance
| # | File | Issue |
|---|------|-------|
| M15 | `replies.py:103-167, 170-212` | Stats and listing load entire reply log on every request; O(n) per page |
| M16 | `unsubscribes.py:55-71` | Every page request re-sorts full log; O(n log n) |
| M17 | `bounce_list.py:141-201` | O(N) scan for paginated bounce listing; page 50 fetches 1250 bounces |
| M18 | `app/main.py:184-205` | Background tasks start immediately with no backoff if ListMonk unreachable |
| M19 | `style.css` (many lines) | Heavy `backdrop-filter: blur()` causes frame drops on low-end devices |

### Accessibility
| # | File | Issue |
|---|------|-------|
| M20 | `index.html` (SVG icons) | SVG icons lack `aria-hidden="true"` or `role="img"` + `aria-label` |
| M21 | `index.html:121-130` | Radio group lacks arrow-key navigation |
| M22 | `index.html:204`, `style.css:2104-2113` | Modal close button touch target ~33×25px, below 44×44px minimum |
| M23 | `login.html` | Missing skip-to-content link |
| M24 | `login.html:495` | Theme toggle button lacks aria-label |
| M25 | `login.html:77` | `overflow: hidden` on body prevents scroll on short viewports |
| M26 | `style.css:2298-2300` | `.notice-info` uses solid purple background with grey text — likely WCAG AA failure |
| M27 | `style.css:1277-1309` | Light theme badge colors may fail contrast on tinted backgrounds |
| M28 | `style.css:1997-2032` | File drop zone has no `:focus-visible` style |

### Frontend JavaScript
| # | File | Issue |
|---|------|-------|
| M33 | `api.js:29-32` | 401 redirect races with pending fetches — multiple concurrent 401s each navigate and toast |
| M34 | `analytics.js:365-433` | Race condition between `loadCampaignSideStats` and `loadCampaignAnalytics` sharing `_sideSeq` counter |
| M35 | `analytics.js:386-390` | Date change bumps seq but doesn't trigger reload — charts disappear until user clicks Load |
| M36 | `charts.js:170,244` | Dashboard campaign rows have onclick but no keyboard support (no tabindex/role/keydown) |
| M37 | `converter.js:692-773` | Import polling uses recursive setTimeout with no cleanup on navigation away |
| M38 | `replies.js:431-435` | `toggleLead` makes 3 API calls (updateStatus + loadStats + loadReplies) for a single toggle |
| M39 | `settings.js:30-31` | `_refreshInterval` not cleared immediately on navigation away (up to 30s delay) |
| M40 | `subscribers.js:224-263` | `showDetail` doesn't check staleness — modal can appear on wrong page after navigation |
| M41 | `subscribers.js:373-381` | Single-subscriber blocklist has no confirmation dialog (all other destructive actions do) |
| M42 | `unsubscribes.js:437-451` | `deleteRecord` uses array index as identifier — wrong record deleted if list changes between render and click |

### Code Quality
| # | File | Issue |
|---|------|-------|
| M43 | `csv_converter.py:27-30` | Global `csv.field_size_limit(sys.maxsize)` affects ALL csv parsing in process |
| M44 | `app_settings.py:33-49` | Blocking I/O (`_atomic_write_json` with fsync) called from async context without `to_thread` |
| M45 | `docker-compose.yml:15` | Healthcheck hits `/auth/login` (template rendering) instead of lightweight `/health` endpoint |
| M46 | `login.html:12-441` | ~430 lines of CSS duplicated inline instead of referencing `style.css` |

---

## Low Severity Issues

| # | Category | Issue |
|---|----------|-------|
| L1 | Security | No global request body size limit configured |
| L2 | Security | Transitive dependencies not pinned in requirements.txt |
| L3 | Security | `order_by` params passed to ListMonk without whitelist validation |
| L4 | Security | GET `/auth/logout` still exists (redirects without clearing session) |
| L5 | Bug | `search_query.py` strips `%`, `_`, `\` instead of escaping — "100%" search silently modified |
| L6 | Bug | `reply_log.py:145-155` — Processed ID seeding from log misses records without message_id |
| L7 | Bug | `unsubscribes.py:91-116` — `campaign_id = 0` grouped under "unknown" due to falsy check |
| L8 | Bug | `bulk_ops.py:66-78` — Result dict mutated from concurrent coroutines (GIL-safe but fragile) |
| L9 | Performance | `csv_converter.py:598` — Redundant encoding detection (file decoded twice) |
| L10 | Performance | `unsubscribe_log.py:134-147` — `save_processed_emails` re-reads file unnecessarily |
| L11 | UX | Chart.js loaded synchronously in `<head>` blocking HTML parsing |
| L12 | UX | No `<noscript>` fallback — blank page with JS disabled |
| L13 | UX | No print stylesheet — dark backgrounds waste ink |
| L14 | UX | Select dropdown arrow SVG hardcoded color doesn't adapt to token changes |
| L15 | UX | `.stats-grid` min column width 160px may be too narrow for large numbers |
| L16 | UX | Table scroll shadow gradient uses fixed dark color, harsh on light theme |
| L17 | Code Quality | `campaign_scheduler.py:93-94` — `import os/tempfile` between function definitions |
| L18 | Code Quality | `_configure_logging` called at module level — side effect on import |
| L19 | Code Quality | `start.sh` kills processes on port 8000 without verifying they're this app |
| L20 | Code Quality | Z-index scale informal and undocumented |
| L21 | Code Quality | Custom scrollbar styles WebKit-only; Firefox gets default scrollbars |
| L22 | Code Quality | `!important` overuse in toggle styles suggests specificity conflicts |
| L23 | Accessibility | Error banner `display:none` → `flex` transition may not trigger aria-live reliably |
| L24 | Accessibility | Submit button disabled state uses inline style instead of CSS class |
| L25 | Frontend | `api.js:76-85` — Blob URL never revoked on error path in `downloadBlob` |
| L26 | Frontend | `app.js:30-45` — Event listeners on navMenu/navMq never cleaned up (stacking if init called twice) |
| L27 | Frontend | `analytics.js:684-691` — Theme change listener re-renders charts without checking page staleness |
| L28 | Frontend | `campaigns.js:170-183` — `showCreate` swallows list/template fetch errors silently |
| L29 | Frontend | `campaigns.js:548-558` — Test send email not validated as email format client-side |
| L30 | Frontend | `charts.js:67` — `lastRepStats` persists across mode switches |
| L31 | Frontend | `converter.js:341-373` — No file size limit check before upload |
| L32 | Frontend | `deliverability.js:431-446` — `svgIcon` and `statusMeta` are global functions polluting namespace |
| L33 | Frontend | `replies.js:437-443` — `copyEmail` doesn't handle clipboard API unavailability (HTTP dev) |
| L34 | Frontend | `subscribers.js:216` — Create form has no list selection; must create then edit to add lists |
| L35 | Frontend | `unsubscribes.js:430-434` — Detail view pagination doesn't bump seq (stale results possible) |
| L36 | Frontend | `unsubscribes.js:15,378-392` — Email-based selection keys cause collision when same email appears multiple times |

---

## Test Coverage Gaps

### Critical Missing Tests
1. **Authentication flow** — No end-to-end login→session→access test; every test patches `verify_session` to return True
2. **Subscriber CRUD** — Create/update/delete/get-by-id completely untested
3. **Template CRUD** — Zero tests for any template endpoint
4. **List CRUD** — Only bulk delete tested
5. **Campaign lifecycle** — Create/update/status/preview untested
6. **Unsubscribe reset** — Function tested but not through HTTP layer
7. **Auto-unblock service** — Core finding logic untested
8. **Export sanitization** — Unit tested but not through actual export endpoints

### High Priority Missing Tests
- IMAP keyword detection variants
- Sentiment classification edge cases (short replies, mixed signals)
- Hard bounce cache background refresh cycle
- Opener cache in-flight deduplication
- ListMonk client error handling / connection pool exhaustion
- Individual deliverability check rules
- Converter import end-to-end
- Settings effect on scanners (blocklist_enabled, auto_tag_leads)
- CSRF / path traversal / rate limiter window expiry
- Background task lifecycle (startup, crash recovery, shutdown drain)
- Config / environment validation

### Test Quality Issues
- Auth bypass pattern creates false confidence (every test patches auth)
- Duplicate `client` fixture defined in 11 test files
- Mock-behavior assertions instead of system-behavior verification
- Opaque regression file names (`test_fixes_round2.py`, etc.)
- Time-dependent fragility with hardcoded Oct 2026 dates
- Frontend tests are structural only (string matching in JS files)
- No performance/load tests

---

## Recommendations by Priority

### 🔴 Immediate (This Week)
1. Fix `reply_log.py` race conditions (C1) — add locks to all mutation functions
2. Create `app/__init__.py` (C2)
3. Fix formula injection whitespace bypass (M6)
4. Fix reply campaign_id type comparison (H13)
5. Add date validation to analytics endpoints (H12)
6. Fix hard_bounce_cache naive datetime (H8)
7. Replace raw `fetch` in unsubscribes.js with `API.request` (H14)
8. Fix index-based delete in unsubscribes.js (M42)
9. Add staleness check to subscribers.showDetail (M40)

### 🟡 Short-Term (This Month)
10. Add CSRF protection to API endpoints (C3)
11. Enforce non-default credentials at startup (C5)
12. Add Content-Security-Policy header (H3)
13. Implement modal focus traps (H4)
14. Make onboarding cards keyboard accessible (H5)
15. Replace inline event handlers with addEventListener (H6)
16. Add `/health` endpoint for Docker healthcheck (M45)
17. Validate `data_path()` against traversal (M2)
18. Persist rate limiter state or add startup warning (C4)
19. Add request timeout to API.request (H15)
20. Add confirmation to single-subscriber blocklist (M41)
21. Fix analytics date change UX (M35)
22. Fix 401 redirect race in api.js (M33)

### 🟢 Medium-Term (Next Quarter)
23. Add Pydantic models for CRUD create/update validation (M7)
24. Cache reply stats/listing to avoid O(n) per request (M15)
25. Add missing test coverage for subscriber/template/list/campaign CRUD
26. Add end-to-end auth flow tests
27. Unify login page CSS tokens with main app (H11)
28. Add `Permissions-Policy` header (M5)
29. Address accessibility issues (M20–M28, M36)
30. Add exponential backoff to background tasks (M18)
31. Move blocking I/O to `asyncio.to_thread` (M44)
32. Reduce toggleLead from 3 API calls to 1 (M38)
33. Fix analytics race condition with separate seq counters (M34)
34. Clean up converter.js import polling on navigation (M37)

---

## Files Reviewed

### Backend Routers (10 files)
`appsettings.py`, `bounces.py`, `campaigns.py`, `converter.py`, `deliverability.py`, `lists.py`, `replies.py`, `subscribers.py`, `templates.py`, `unsubscribes.py`

### Backend Services (21 files)
`app_settings.py`, `auto_unblock.py`, `bounce_filters.py`, `bounce_ingest.py`, `bounce_list.py`, `bulk_ops.py`, `campaign_scheduler.py`, `csv_converter.py`, `deliverability_service.py`, `export_service.py`, `hard_bounce_cache.py`, `imap_helpers.py`, `imap_reply_tracker.py`, `imap_unsubscribe.py`, `link_unsubscribe.py`, `listmonk_client.py`, `opener_cache.py`, `reply_log.py`, `search_query.py`, `task_utils.py`, `unsubscribe_log.py`

### Frontend JavaScript (12 files)
`api.js`, `app.js`, `analytics.js`, `campaigns.js`, `charts.js`, `converter.js`, `deliverability.js`, `lists.js`, `replies.js`, `settings.js`, `subscribers.js`, `unsubscribes.js`

### Infrastructure (9 files)
`auth.py`, `config.py`, `main.py`, `__init__.py`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `start.sh`, `requirements.txt`

### Templates & Styles (3 files)
`index.html`, `login.html`, `style.css`

### Tests (21 files)
All test files in `tests/` directory plus `conftest.py`

**Total: 76 files reviewed**
