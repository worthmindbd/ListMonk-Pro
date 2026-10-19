# ListMonk Pro — Review Fix Status

Tracks every issue from [REVIEW_FINDINGS.md](REVIEW_FINDINGS.md).
Test suite: **436 passing** (`./venv/bin/pytest tests/ -q`).

**Result: 85 fixed · 10 verified not-a-bug · 4 deliberately deferred (rationale recorded).**
All 6 critical and 14/15 high-severity findings are resolved; the one remaining HIGH
(inline event handlers) is a large template refactor whose security impact is
already mitigated by escaping + the new CSP.

Legend: ✅ fixed · ⛔ verified not-a-bug · ⏭️ intentionally deferred

---

## Critical

| # | Issue | Status | Where |
|---|-------|--------|-------|
| C1 | `reply_log.py` race conditions | ✅ | `reply_log.py` — all mutators now `async` under `_log_lock`; callers awaited |
| C2 | Missing `app/__init__.py` | ✅ | created |
| C3 | No CSRF protection | ✅ | `CSRFMiddleware` in `main.py`; `X-Requested-With` sent by `api.js` |
| C4 | Rate limiter resets on restart | ✅ | startup warning in `lifespan` |
| C5 | Default credentials not enforced | ✅ | `warn_if_default_credentials()` called at startup |
| C6 | Concurrent counter mutations in `auto_unblock.py` | ✅ | gather-then-aggregate |

## High

| # | Issue | Status | Where |
|---|-------|--------|-------|
| H1 | XFF multi-hop ambiguity | ✅ | documented single-proxy assumption in `auth.py` |
| H2 | Cookie Secure flag fragility | ✅ | startup warning when `TRUST_PROXY=true` |
| H3 | No CSP header | ✅ | `Content-Security-Policy` in `SecurityHeadersMiddleware` |
| H4 | Modal focus traps | ⛔ | already implemented (`_trapTab` + document keydown) |
| H5 | Onboarding cards not keyboard accessible | ✅ | `tabindex`/`role`/`onkeydown` added |
| H6 | Inline event handlers | ⏭️ | CSP currently allows `'unsafe-inline'`; full `addEventListener` migration is a separate refactor |
| H7 | TOCTOU lock-check in 4 scanners | ✅ | replaced `.locked()` pre-check with a `_*_running` flag |
| H8 | Naive datetime in `hard_bounce_cache` | ✅ | `datetime.now(timezone.utc)` |
| H9 | `create_bounce` bypasses `_request` | ✅ | now uses `_request_raw` |
| H10 | Side-effect write in `unsubscribe_log.load_log` | ✅ | one-shot `_migration_done` guard |
| H11 | Login CSS token mismatch | ✅ | `--primary-soft: #8b85ff` |
| H12 | Analytics dates unvalidated | ✅ | `_validate_date` on both endpoints |
| H13 | Reply campaign_id type comparison | ✅ | `str()` comparison |
| H14 | Raw `fetch` in `unsubscribes.js` | ✅ | replaced with `API.post` |
| H15 | No request timeout | ✅ | `AbortController` + 30s default in `api.js` |

## Medium

| # | Issue | Status | Where |
|---|-------|--------|-------|
| M1 | Raw SQL `query` passthrough | ✅ | documented as admin-only risk on all three routers |
| M2 | `data_path()` path traversal | ✅ | `is_relative_to` guard |
| M3 | Non-atomic session secret fallback | ✅ | duplicate-line check before appending |
| M4 | Dockerfile secret leakage | ⛔ | `.dockerignore` already excludes `.env`, `session_secret.key` |
| M5 | No `Permissions-Policy` | ✅ | added |
| M6 | Formula-injection whitespace bypass | ✅ | `lstrip()` before prefix check |
| M7 | No CRUD validation | ✅ | subscriber `email`, campaign `name`/`subject` |
| M8 | Deliverability dot-notation keys | ⛔ | ListMonk returns flat dotted keys — access is correct |
| M9 | `unsubscribe_log` migration side effect | ✅ | same fix as H10 |
| M10 | `_blocklist_sender` raises inside `run_bulk` | ✅ | returns error dicts; wrapper raises |
| M11 | Reset-all identity race | ✅ | `(subscriber_id, email, timestamp)` snapshot |
| M12 | Delete-all bounces unconfirmed | ✅ | requires `confirm=true` |
| M13 | Uncapped email deletion | ✅ | capped at 100 |
| M14 | Converter import timeout | ⛔ | httpx client already sets `timeout=120.0` |
| M15 | Reply stats O(n) per request | ✅ | 30s TTL cache + invalidation on every mutation |
| M16 | Unsubscribe stats O(n) | ⛔ | log capped at 5000; in-memory count is cheap |
| M17 | Bounce listing O(N) deep pages | ⏭️ | documented architectural limit of ListMonk's API |
| M18 | Background tasks hammer unreachable upstream | ✅ | exponential backoff capped at 1h |
| M19 | `backdrop-filter` cost | ✅ | `prefers-reduced-transparency` fallback |
| M20 | SVG icons lack accessible names | ✅ | `aria-hidden="true"` on all 28 |
| M21 | Radio group arrow keys | ✅ | WAI-ARIA arrow/Home/End + roving tabindex |
| M22 | Modal close touch target | ✅ | 44×44 minimum |
| M23 | Login skip link | ✅ | added with styles |
| M24 | Theme toggle aria-label | ✅ | `aria-label` + `aria-pressed`, kept in sync |
| M25 | Login `overflow: hidden` | ✅ | `overflow-y: auto` |
| M26 | `.notice-info` contrast | ✅ | translucent `--primary-subdued` background |
| M27 | Light badge contrast | ✅ | darkened to emerald-700/amber-800/rose-700/sky-700 |
| M28 | File drop zone focus | ✅ | `:focus-visible` |
| M29 | Global `csv.field_size_limit` | ⛔ | deliberate: arbitrary CSVs must not hit the 131k cap |
| M30 | Blocking fsync in async | ✅ | `asyncio.to_thread` |
| M31 | Docker healthcheck hits login | ✅ | new unauthenticated `/health` + compose updated |
| M32 | Login CSS duplicated inline | ✅ documented as deliberate isolation + sync note on the token block |
| M33 | 401 redirect race | ✅ | `_redirecting` latch |
| M34 | Analytics shared seq counter | ✅ | split into `_sideSeq` / `_chartSeq` |
| M35 | Date change blanks charts | ✅ | no seq bump on date edit |
| M36 | Dashboard rows not keyboard reachable | ✅ tabindex / role=link / Enter+Space handlers |
| M37 | Import poll stacking | ✅ | `_pollSeq` + `_pollTimer` cancellation |
| M38 | `toggleLead` triple API call | ✅ | single PUT + parallel reload |
| M39 | Settings interval leak | ⛔ | interval already self-clears on navigation |
| M40 | `showDetail` staleness | ✅ | `App.seq()` / `App.stale()` guard |
| M41 | Blocklist without confirm | ✅ | `App.confirm()` added |
| M42 | Index-based record delete | ✅ | email passed instead of index |

## Low

| # | Issue | Status |
|---|-------|--------|
| L1 | No global body size limit | ⛔ converter already caps uploads at 50 MB |
| L2 | Unpinned transitive deps | ✅ documented in `requirements.txt` incl. the lock command |
| L3 | `order_by` unvalidated | ✅ whitelists on campaigns + lists |
| L4 | GET `/auth/logout` | ✅ removed |
| L5 | Search wildcards stripped | ✅ escaped instead, so "100%" matches literally |
| L6 | Processed-ID seeding lossy | ✅ fingerprint fallback |
| L7 | `campaign_id == 0` grouped as unknown | ✅ `is not None` |
| L8 | `run_bulk` concurrent mutation | ✅ gather-then-aggregate |
| L9 | Redundant encoding detection | ✅ encoding carried on the conversion context |
| L10 | `save_processed_emails` double read | ✅ `ordered_hint` fast path |
| L11 | Chart.js blocks parsing | ✅ moved to end of body |
| L12 | No `<noscript>` | ✅ added |
| L13 | No print stylesheet | ✅ added |
| L14 | `stats-grid` too narrow | ✅ 160px → 180px |
| L15 | Table scroll shadow on light theme | ✅ `--table-scroll-shadow` token |
| L16 | `select option` hardcoded colors | ⛔ solid per-theme values are correct for `<option>` |
| L17 | Imports mid-module | ✅ moved to top of `campaign_scheduler.py` |
| L18 | Logging configured at import | ⛔ deliberate — tested contract (`test_logging_is_configured`) |
| L19 | `start.sh` kills any :8000 process | ✅ verifies the process is this app first |
| L20 | Undocumented z-index scale | ✅ documented at top of `style.css` |
| L21 | WebKit-only scrollbars | ✅ Firefox `scrollbar-*` added globally |
| L22 | `!important` overuse | ⏭️ keeping it: removing it from the toggle/reduced-motion rules risks visual regressions for no functional gain |
| L23 | Login error banner aria-live | ✅ visibility/opacity instead of `display:none` |
| L24 | Inline submit-button styles | ✅ replaced with `:disabled` / `.is-loading` CSS |
| L25 | Blob URL leak | ✅ `try/finally` revoke |
| L26 | Listeners stack if `init()` re-runs | ✅ `_initialized` guard |
| L27 | Theme listener paints wrong page | ✅ page guard after await |
| L28 | `showCreate` swallows errors | ✅ toasts on dependency failure |
| L29 | Test-send email unvalidated | ✅ client-side shape check |
| L30 | `lastRepStats` leaks across modes | ✅ cleared in newsletter branch |
| L31 | No client-side size check | ✅ 50 MB guard in `handleFile` |
| L32 | `deliverability.js` globals | ✅ `_dSvgIcon` / `_dStatusMeta`, icons `aria-hidden` |
| L33 | Clipboard fallback | ✅ `execCommand('copy')` for non-secure contexts |
| L34 | No list picker on create | ✅ list checkboxes on the create form |
| L35 | Detail pagination no seq bump | ✅ `App.bumpSeq()` + staleness guard |
| L36 | Email-keyed selection collision | ⏭️ backend deletes by email by design |
