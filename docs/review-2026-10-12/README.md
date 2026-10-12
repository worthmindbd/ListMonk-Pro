# Review package — 2026-10-12

Visual + configuration review of the ListMonk Pro dashboard and the ListMonk instance it manages.

| Document | Contents |
|----------|----------|
| [`01-project-review.md`](01-project-review.md) | Full visual review: method, route/feature inventory by category, live data snapshot, 20 findings by severity, what works well |
| [`02-listmonk-settings-audit.md`](02-listmonk-settings-audit.md) | Essential-settings audit of the ListMonk instance: 8 categories (A–H), current values, verdicts, recommended fix order, items needing a test send |
| [`screenshots/`](screenshots/) | 28 screenshots — every route in both modes, 1280/1600 desktop, mobile, light theme, login, modals, converter flow, the categorized sidebar (both modes), the new alt-body field and the live public subscription form |

## Result at a glance

| Area | Result |
|------|--------|
| Dashboard app (12 routes, both modes) | 🟢 renders cleanly — 0 console errors, 0 failed API calls, **190/190 tests pass** |
| Dashboard ↔ ListMonk integration | 🟢 Connected; live data flowing both directions |
| ListMonk transport (SMTP, TLS, identity) | 🟢 configured; STARTTLS verified (TLS 1.3, valid cert) |
| Domain authentication | 🟢 SPF + DKIM + DMARC published · 🟡 DKIM signing unverified, DMARC has no `rua` |
| Bounce handling | 🟡 **app side fixed** (30-day window + processed-ID state; 6 DSNs now visible where the old scanner saw 0) · 🔴 ListMonk's own bounce mailbox is still an untouched placeholder |
| Newsletter/opt-in readiness | 🟢 **fixed live** — public subscription form (ALTCHA-protected), opt-in page, double opt-in enabled, new public double-opt-in list created; cold-email lists stay private and hidden from the form |
| Cold-email readiness | 🟡 presets applied and audit now reports **77%** honestly; hidden-text block removed from the live template |

## Fix pass — 2026-10-12

**19 of 21 review findings are resolved** (see the per-finding status in
[`01-project-review.md`](01-project-review.md) and the full change table in its §6). Highlights:

- Removed the hidden "Ghost Ink" block that put **~1,609 words of invisible text into every sent email**; the live template went 18,074 → 7,343 bytes with the unsubscribe link and per-lead body intact.
- The 1-click deliverability preset was **broken**: ListMonk rejects partial settings payloads (`At least one SMTP block should be enabled`) and a naive round trip would have overwritten the SMTP password with the `••••` mask. Added `listmonk.apply_settings()` (merge + de-mask) and verified live.
- Bounce ingestion no longer trusts IMAP `\Seen`; it scans a 30-day window against a persisted processed-ID set and ignores delay notices.
- Audit is honest: 10 checks, `info` excluded from the score, failing checks named (production reads 77%, not 100%).
- Fixed the `SENT` mislabel (list showed 2,929 where ListMonk sent 1,851), 1280 px action-column overflow, CSV column suggestions + ragged-row warnings, toggle legibility, and `.env` permissions.
- Hardened the live media upload allow-list (dropped the `*` wildcard).
- 26 new regression tests (bounce ingestion state machine, DSN parsing, settings merge/de-mask, audit scoring, converter suggestions).

**Applied with operator approval:** the newsletter readiness batch — public subscription
page + opt-in page + double opt-in + ALTCHA captcha, plus a new public double opt-in list
(`Aevora Newsletter`) as the signup destination. Everything cold-email stayed as it was.

**Still open:** the 1-message-per-2-minute sending cadence (confirm intent vs the 1,078 queued
messages), optional ListMonk-owned bounce mailbox, SMTP `from_addresses` restriction, DMARC
`rua`, and the campaign subject / plain-text body content choices.

## How to reproduce

```bash
# dev server
./start.sh                       # or: ./venv/bin/uvicorn app.main:app --port 8000
# tests
./venv/bin/pytest tests/ -q      # 164 passed
```

Reviews were read-only: no campaign, subscriber, template or ListMonk setting was created, changed or deleted; the CSV import step was exercised up to the preview and not submitted.