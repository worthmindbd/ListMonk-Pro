# ListMonk Pro — Full Visual Review

**Date:** 2026-10-12
**Reviewer:** automated browser review (agent-browser, Chromium via CDP)
**Scope:** every route of the dashboard SPA, both operating modes, all interactive surfaces, live ListMonk data
**Environment:** local dev server (`uvicorn app.main:app`, `http://127.0.0.1:8000`) against the **live** ListMonk instance `https://lm.getaevoramedia.com` (read-only — no destructive action was executed)

---

## 1. How the review was performed

| Step | Method | Result |
|------|--------|--------|
| Page sweep | Visited all 12 hash routes at 1600×1400 and 1280×900, screenshotted full page | 12/12 routes rendered |
| Console | `console` capture across a full navigation sweep | **0 JS errors / warnings** |
| Network | Request log filtered to `/api/*` with 4xx/5xx filters | **0 failed API calls** |
| Interaction | Login, mode switch, tabs, search, filters, modals, edit forms, file upload, pagination | all functional |
| Themes | Dark ↔ light toggle, persisted in `localStorage` | both themes complete |
| Responsive | iPhone 14 emulation + 1280/1600 desktop | mobile OK, 1280 has table overflow |
| Auth | Session cookie flags, unauth API/HTML behaviour, bad-credential response | correct |
| Tests | `./venv/bin/pytest tests/ -q` | **164 passed** |

Screenshots live in `docs/review-2026-10-12/screenshots/`.

---

## 2. Route & feature inventory (categorized)

### 2.1 Shared pages (both modes)

| Route | Page title | Features verified | Screenshot |
|-------|-----------|-------------------|-----------|
| `#/dashboard` | Dashboard | 4 stat cards, bar chart, doughnut chart, recent campaigns table | `v2-dashboard.jpg`, `cold-dashboard.jpg` |
| `#/campaigns` | Campaigns / *Cold Outreach* | search, status filter, export, new campaign, detail view, status actions | `v2-campaigns.jpg`, `campaign-detail.jpg` |
| `#/subscribers` | Subscribers / *Leads* | search, pagination (50/page), add, edit (attributes JSON), delete, export | `v2-subscribers.jpg`, `subscriber-edit-modal.jpg` |
| `#/lists` | Lists / *Lead Lists* | list table (type/optin/subs/status/tags), create, edit, delete | `v2-lists.jpg`, `cold-lists.jpg` |
| `#/analytics` | Analytics | 5 summary cards, campaign comparison chart, per-campaign drill-down with date range + Load, 4 chart panels with CSV exports | `v2-analytics.jpg` |
| `#/bounces` | Bounces | campaign filter, hard/soft type filter, count, ingest trigger, export, bulk delete | `v2-bounces.jpg` |
| `#/unsubscribes` | Unsubscribes | IMAP monitor panel, 3 stat cards, campaign grouping, export all, undo all, scan now | `v2-unsubscribes.jpg` |
| `#/converter` | CSV Converter | 3-step wizard: upload → map columns → preview/convert, attribute detection | `converter-mapping.jpg`, `converter-clean-preview.jpg` |
| `#/settings` | Settings | tab 1 Campaign Scheduler (timezone, window, day pills, timeline, save), tab 2 Auto-Unblock (status, manual run) | `v2-settings.jpg`, `settings-autounblock2.jpg` |

### 2.2 Newsletter-mode-only

| Route | Features verified | Screenshot |
|-------|-------------------|-----------|
| `#/templates` | 7 templates, type badges (campaign / campaign_visual / tx), default flag, preview, edit, set-default | `v2-templates.jpg` |

### 2.3 Cold-email-mode-only

| Route | Features verified | Screenshot |
|-------|-------------------|-----------|
| `#/replies` | 5 sentiment cards, sentiment/status filters, search, scan now, reply feed with body, mark-as-hot-lead, copy email, full details, blocklist | `cold-replies.jpg` |
| `#/deliverability` | 100% score dial, 6-row pre-flight audit table, 1-click presets (cold email / newsletter), 3 explainer cards | `cold-deliverability.jpg` |

### 2.4 Sidebar navigation — categorized (reorganized in the fix pass)

The sidebar was a flat list of 11 mode-dependent entries; it is now grouped into five
categories that keep working in both modes (mode-specific pages stay hidden and a category whose
items are all hidden collapses, so no empty headings remain):

| Category | Newsletter mode | Cold Email mode |
|----------|-----------------|-----------------|
| **Overview** | Dashboard, Analytics | Dashboard, Analytics |
| **Audience** | Subscribers, Lists, Unsubscribes | Leads, Lead Lists, Unsubscribes |
| **Outreach** | Campaigns, Templates | Cold Outreach, Replies & Leads |
| **Deliverability** | Bounces | Deliverability, Bounces |
| **Tools** | CSV Converter, Settings | CSV Converter, Settings |

Implementation: `templates/index.html` (`.nav-section-title` + `data-nav-group` per entry),
`static/css/style.css` (heading style using the contrast-checked `--text-muted` token), and
`static/js/app.js` (`setMode()` hides a heading when its category has no visible items).

### 2.5 Mode switcher behaviour (verified live)

| Element | Newsletter mode | Cold Email mode |
|---------|----------------|-----------------|
| Sidebar label `Subscribers` | Subscribers | **Leads** |
| Sidebar label `Lists` | Lists | **Lead Lists** |
| Sidebar label `Campaigns` | Campaigns | **Cold Outreach** |
| `Replies & Leads`, `Deliverability` | hidden | visible |
| `Templates` | visible | hidden |
| Dashboard layout | subscribers/lists/campaigns/bounces cards + status doughnut | leads/dispatched/replies/hot-leads cards + sentiment doughnut + deliverability banner |
| Accent colour | indigo | green |

Persistence: `localStorage.listmonk_pro_mode`, first-run onboarding modal (`01-onboarding.jpg`).

### 2.6 Design system (as implemented)

- Dark theme default, light theme fully implemented (`theme-light-dashboard.jpg`), toggle state persisted.
- Sidebar navigation with SVG icons, ambient gradient mesh orbs, glass-morphism cards, segmented mode pill, toasts, modal + inline forms.
- Typography: Plus Jakarta Sans / Outfit / Inter (Google Fonts), Chart.js 4.5.1 vendored locally.
- Mobile (iPhone 14): sidebar collapses behind a hamburger, cards stack single-column (`mobile-dashboard.jpg`).

---

## 3. Live data snapshot (production instance)

| Metric | Value |
|--------|-------|
| Subscribers | 1,931 (1,928 enabled, 3 blocklisted, 0 orphans) |
| Lists | 2 — `Default list` (1,931, tag `test`), `Default list (Remaining Leads)` (996, tag `campaign-1-remaining`); both **private / single opt-in** |
| Campaigns | 2, both `finished` — #3 sent 935 of 1,933; #4 sent 916 of 996 |
| Messages sent (ListMonk) | 1,851 |
| Bounces recorded in ListMonk | **0** |
| Templates | 7 (1 default campaign, 1 archive, 1 `tx` sample, 1 campaign_visual, 3 `sequence_*`) |
| Replies tracked | 2 (both sentiment *Question*), reply rate 0.11% |
| Unsubscribes logged | 3 (all `link` source, 0 email replies) |
| Hard-bounce cache | empty (0 campaigns) |
| IMAP monitor | `IMAP CONNECTED` |
| Deliverability audit score | 100% (6/6 checks pass) |

Lead enrichment is genuinely rich: sampled 60 subscribers — **100%** carry `company_name`, `aevora_segment`, `outreach_tier`, `copy_variant`, `research_notes/signal/urls`, `linkedin_posts_found` and per-touch personalisation (`email_1_subject/body`, `email_2_*`, `email_3_*`).

---

## 4. Findings

Severity: **P1** = wrong data or deliverability/compliance risk · **P2** = broken/ineffective pipeline · **P3** = UX, hygiene, polish.

### P1 — Data correctness

**1. The `SENT` column on the Campaigns list shows the *audience size*, not the sent count.**

✅ **Fixed.** `campaigns.js` uses `c.sent`; the audience size moved to a tooltip. Verified live: the list shows 916 / 935.
`static/js/campaigns.js:57` renders `const sent = c.to_send || 0;`. Evidence: the list shows `996` / `1,933` under `SENT`, while the same campaigns' detail view shows `TO SEND 996 / SENT 916` and `TO SEND 1,933 / SENT 935`, and the Analytics page totals 1,851. ListMonk's own API returns `sent = 916/935`, `to_send = 996/1933`.

**2. The newsletter-mode dashboard chart labelled `Sent` also plots `to_send`.**

✅ **Fixed.** `charts.js` plots `c.sent`; the newsletter dashboard and Analytics now agree.
`static/js/charts.js:292` — `const sent = recent.map(c => c.to_send || 0);`. The cold-email dashboard, Analytics page, and the *Recent campaigns* table (`charts.js:190`, `c.sent || c.to_send`) all correctly use `sent`, so the same dashboard reports 2,929 vs 1,851 dispatched depending on mode.

### P1 — Deliverability & compliance exposure

**3. Every campaign email contained ~1,609 words of invisible text.**

✅ **Fixed.** Ghost Ink block removed from `sequence_1_template` (backup taken first); the rendered preview is 7.3 KB of normal HTML with the unsubscribe link intact.
`sequence_1_template` (id 5) embedded a `<div data-gi="1" style="color:transparent;font-size:0px;line-height:0px;max-height:0px;mso-hide:all;overflow:hidden;display:none;opacity:0;">` whose own comment described it as "positioned at top of card so spam/NLP filters evaluate 280+ words of human text in active content zone".

Closer inspection during the fix showed the block was **unconditional**: `{{ if ghost_ink_1 }} … {{ else if ghost_ink }} … {{ else -}}` fell through to six hard-coded paragraph sets selected by `mod subscriber_id 6`. Since no subscriber carries `ghost_ink` attributes, the `else` branch always rendered — so **all 1,851 sent emails carried ~1,609 words of hidden, per-subscriber-varied filler text**. Hidden/transparent text is exactly what Gmail and Microsoft content filters detect, and it contradicted the in-app “Deliverability Health: 100%” badge (the audit only evaluated ListMonk settings, never the template). The block has been removed and the template now renders 7.3 KB of normal HTML. (Earlier in this review I mis-stated this as "dormant" after finding no `ghost_ink` attributes — the fallback branch is why that was wrong.)

**4. The per-lead personalised subject line is unused.**

⏳ **Open — content decision.** Point the campaign subject at `{{ .Subscriber.Attribs.email_1_subject }}` (company-name fallback) to use the copy already generated per lead.
Campaign subjects for #3 and #4 are both `{{ .Subscriber.Attribs.company_name }}`, so every inbox shows just a company name (`Framediscreet`, `Rocket Saas`). The enrichment pipeline produces `email_1_subject` per lead and the template even uses it in the HTML `<title>`. Setting the campaign subject to `{{ .Subscriber.Attribs.email_1_subject }}` (with a company-name fallback) would use the copy that was already generated.

**5. 1,078 of 2,929 queued messages were never delivered, with no visible reason.**

⏳ **Open — needs the ListMonk log, but the mechanism is now pinned down.** Reading ListMonk's
`manager.pipe.cleanup()` settles what a status can mean:
`withErrors` → status **paused** (+ "Too many errors" notification), a manual stop → status left
as **paused/cancelled**, otherwise **finished**. So `finished` with `sent < to_send` means
(a) fewer than 10 send errors *and* (b) nobody stopped it *and* (c) the subscriber query simply
stopped returning rows at 935. That points at the queue draining early rather than at send
failures — e.g. recipients removed from the list / unsubscribed between campaign creation and
send, or a stop-and-resume history where `to_send` still holds the initial total
(`to_send` is written at creation from `COUNT(DISTINCT subscriber_id)` and also recomputed when a
running campaign is picked up). Confirm in the ListMonk log — look for
`campaign (...) finished`, `set campaign (...) to paused`, `messages exceeded (...) for the window`
and `error sending message in campaign ...` — and in the admin notification mail sent to
`admin@worthmind.net`.
Campaign #3: 935 of 1,933 · campaign #4: 916 of 996 — yet both campaigns ended as `finished` (not `paused`), ListMonk reports **0** bounces, and the dashboard surfaced no send errors. Two candidate mechanisms, both worth checking in the ListMonk log (Settings → Logs / `docker logs listmonk`):

- the sliding-window throttle (audit item A6) caps sending at 1 message / 2 minutes ≈ 720/day. The observed volumes match almost exactly (935 ≈ 31 h, 916 ≈ 30.5 h, versus ~32 h between campaign #3's start and #4's start). **Recommendation: keep it.** 720/day is a defensible single-domain pace for a 1,931-lead list (one touch ≈ 2.7 days), and the 2-minute window is *better* than a wider window on one important axis: `pushBatch` sleeps out the remainder of the window inside the send loop, so a 1-hour window would delay pause/resume by up to an hour, whereas 2 minutes bounds it. What was missing was visibility, not speed — the audit now reports the cadence (`≈720 msgs/day`) as an INFO row.
- `app.max_send_errors = 10` pauses a campaign after 10 cumulative send errors; a `paused` campaign would not read `finished`, so more than 10 SMTP-level failures is inconsistent with the recorded status — meaning the missing messages are more likely still queued (campaign stopped early) than rejected.

Either way, "SENT 935 of 1,933" needs a first-class explanation in the UI. The audit now
reports the effective cadence (`≈720 msgs/day` for this instance) so the consequence of the
throttle is visible before the next send.

**6. HTML-only mail, no plain-text alternative.**

✅ **Fixed in the app.** The campaign create/edit forms now have a **Plain-text alternative body**
field (stored as ListMonk's `altbody`), so future campaigns can ship a matching text part. Verified
end to end: created a throwaway draft through the UI, confirmed ListMonk stored the alt body and
that the edit form pre-fills it, then deleted the draft. The subject field also gained a hint listing
the usable placeholders (`{{ .Subscriber.Attribs.email_1_subject }}`, `company_name`, `Name`).
Note this only helps campaigns created **in this dashboard** — campaigns built by an external
pipeline must set `altbody` themselves.
Both campaigns have an empty `altbody` and an 18 KB HTML template with one image and no `TrackView` pixel (good), but no `text/plain` part — the single strongest signal gap for primary-inbox placement in cold outreach.

**7. The 1-click preset button was broken (found while fixing the settings above).**

✅ **Fixed.** `listmonk.apply_settings()` merges the preset onto current settings and blanks masked secrets; verified live (SMTP secret intact, all other settings untouched).
`apply_deliverability_preset()` PUT only the preset keys to `/api/settings`. ListMonk
validates that payload as a whole and rejects a partial body with
`400 {"message":"At least one SMTP block should be enabled"}`, so the preset could never
apply. Had it been accepted, echoing the GET response back would also have overwritten the
SMTP password with ListMonk's `••••` mask. Fixed with
`listmonk.apply_settings(patch)` (merge + de-mask), verified live: the upload allow-list
change went through with the SMTP secret's mask length unchanged and every other setting
intact.

### P2 — Bounce pipeline is effectively dead

**8. ListMonk's own bounce processing cannot work as configured.**

✅ **Partly fixed.** The audit reports this as a hard FAIL instead of hiding it behind 100%; configuring a ListMonk bounce mailbox or provider webhook is a ListMonk-side decision.
`bounce.enabled = true`, but `bounce.mailboxes = [{ enabled: false, host: "pop.yoursite.com", username: "username", return_path: "bounce@listmonk.yoursite.com" }]` — the untouched placeholder — and every provider (`sendgrid`, `ses`, `webhooks`, `postmark`, `azure`, `forwardemail`, `lettermint`) is disabled. Consequently `bounce.actions` (`hard → blocklist` after 1) can never fire and ListMonk shows **0 bounces** for 1,851 sends.

**9. The dashboard's own bounce scanner can silently lose bounces.**

✅ **Fixed.** Date-window scan + `bounce_processed.json`. Verified against the live mailbox: 6 DSNs are now examined where the old query saw 0.
`app/services/bounce_ingest.py` searches `(UNSEEN SINCE …)` and marks each message `\Seen` after handling; it keeps **no processed-state file** (unlike `imap_unsubscribe.py` / `imap_reply_tracker.py`, which persist `*_processed.json`). Because the bounce mailbox is a normal mailbox (`kawsar@…`), any DSN a human opens in webmail is already `\Seen` and is never ingested. Live evidence: the bounce INBOX holds exactly 2 DSNs (`noah@90smusicstudio.com` 4.0.0 delay, `producers.ldn@absolutepost.com` 5.0.0 failure — both are real subscribers), both already `\Seen`, and ListMonk holds 0 bounce records.

### P2 — Mode presets are global, mode promises are not

**10. `apply-preset` rewrites instance-wide ListMonk settings.**

ℹ️ **Documented** in `AGENTS.md` (rule 12); the preset still writes global settings by design.
Current state is the cold-email profile (`privacy.unsubscribe_header=false`, `privacy.disable_tracking=true`, `app.enable_public_subscription_page=false`, `app.send_optin_confirmation=false`). Switching to the newsletter preset flips all of it for **both** modes at once. Running newsletter sends today would mean no RFC 8058 `List-Unsubscribe` header (a Gmail/Yahoo bulk-sender requirement) and no compliant opt-in path.

**11. The pre-flight audit overstates safety.**

✅ **Fixed.** 10 checks, informational checks excluded from the score, failing checks named in the banner. Production now reads 77% (honest) instead of 100%.
`app/services/deliverability_service.py` evaluates 6 settings only. It does not check the sending rate / sliding window, bounce-mailbox configuration, DNS (SPF/DKIM/DMARC), or template hygiene (hidden text, alt text). A 100% score therefore only means "these six settings match the cold-email preset".

### P3 — Layout, wording and polish

**12. Action columns overflow at 1280 px.** Subscribers wrapper 1,042 px vs 962 px client width (action cell right edge 1,331 px > viewport), Templates 1,061 px vs 962 px (edge 1,350 px). Horizontal scroll exists (`overflow-x:auto`), but the last button is off-screen without any scroll affordance. Fine at ≥1440 px.

✅ **Fixed.** Sticky action column + tighter cell padding at ≤1440 px. Verified at 1280 px: no overflow on Subscribers, all actions visible on Templates.

**13. CSV converter does not auto-detect the *name* column** — `Full Name` is offered but the Name selector stays `-- None --` (Email *is* auto-detected).

✅ **Fixed.** `detect_columns` returns `suggested`; the mapping step preselects Email Address + Full Name (verified in the browser).

**14. CSV converter keeps stale state across SPA navigation.** Returning to `#/converter` re-shows the previous preview instead of the upload step; only a full page reload resets it.

❌ **Not a bug — review error.** Re-opening the *same* hash is a no-op in an SPA; navigating away and back correctly resets the converter to step 1 (re-verified).

**15. No warning when a row's field count ≠ header count.** A row with a leading empty field was silently shifted (`Company` received the name value). Well-formed files convert correctly (verified end-to-end).

✅ **Fixed.** `ragged_rows` + `warnings` from `detect_columns`, shown as a warning banner in the mapping step and a toast on download.

**16. Campaign list shows the raw Go template as the subject** (`{{ .Subscriber.Attribs.company_name }}`) with no "template" hint; only the detail view makes it obvious.

✅ **Fixed.** Templated subjects get a `tpl` badge plus a tooltip showing the raw template.

**17. The IMAP monitor's blocklist toggle** renders as a bare knob with no track/label, so the on/off state is ambiguous (`Action: Unsubscribe Only` + anonymous pill in `v2-unsubscribes.jpg`).

✅ **Fixed.** Dedicated `--toggle-track` token; verified on and off — the track is legible in both themes.

**18. Theme toggle iconography**: the sun/moon glyphs sit outside the switch instead of inside it.

❌ **Won't fix.** The sun/moon glyphs flanking the switch are the intended design.

**19. Reply quote-stripping is partial.** The Teyvn Chubinski reply still ends with `On Tue, Oct 6, 2026 at 7:49 PM Kawsar Mahmud <kawsar@getaevoramedia.com> wrote:` and `[image:image]` placeholders survive.

✅ **Fixed.** Reply text is tidied (`[image: …]` markers, blank runs) and the quote stripper itself was already corrected in `894cf8f`; stored replies re-normalised from a backup.

**20. `.env` is world-readable (`-rw-r--r--`)** while holding the ListMonk API key, dashboard password and both IMAP passwords. `session_secret.key` is correctly `600`. Recommend `chmod 600 .env`.

✅ **Fixed.** `.env` is now `600`, with a note in `.env.example`.

**21. Data hygiene:** the primary production list is literally named `Default list` and carries the tag `test`; ~45% of sampled leads are tagged `aevora_segment: low_fit` yet were emailed; 29% have role-address names (`Info`, `Hello`, `Customercare`) — `company_name` is the field worth personalising with.

⏳ **Open — data hygiene (recommendation: do NOT mutate the data).** Exact counts from ListMonk:
`low_fit` = **594** of 1,931 (31%; the 45% figure this review first gave came from a 60-subscriber
sample), `creative_marketing` = 1,215, `weak` copy variant = 106, and excluding `low_fit` leaves
**1,335** enabled targets. Rather than deleting/blocklisting ~600 records (destructive, and it
would also hide them from the reply tracker), exclude them at targeting time with
`subscribers.attribs->>'aevora_segment' <> 'low_fit' AND subscribers.status = 'enabled'`.
The list rename/tag cleanup is deliberately left alone too: the app and any external pipeline may
key off list **names or tags** (`Default list` carries the tag `test`, and campaigns were attributed
by tag-derived lists), so renaming is a coordination step, not a cosmetic one.

---

## 5. What works well

- Zero console errors and zero failed API calls across all 12 routes — a very clean frontend for a no-build vanilla JS SPA.
- Real backend automation is observable and healthy: IMAP unsubscribe monitor connected, reply tracker with sentiment, link-unsubscribe scan attributing campaigns, hard-bounce cache refresh, scheduler and auto-unblock tabs.
- Security posture is deliberate: 401 for unauth API / 302 for pages, `HttpOnly; SameSite=Lax` 7-day session cookie with HMAC-SHA256, login rate limiting, `nosniff` / `SAMEORIGIN` / `same-origin` headers, HSTS only when TLS was actually used.
- The CSV converter is the strongest workflow: delimiter/encoding/column detection, explicit Email/Name mapping, opt-in JSON attributes, preview before either downloading or importing — verified producing `{"Company":"Brightloop","Country":"ES"}` style attribute payloads.
- 190/190 tests pass (164 at review time, +26 regression tests added by the fix pass below).

---

## 6. Fix pass — 2026-10-12

Everything below was implemented and verified after the review. Production-affecting changes
are called out explicitly; the rest are code/CSS changes covered by the test suite.

### Code & UI

| # | Change | Files |
|---|--------|-------|
| 1 | Campaign list shows the real sent count (audience in a tooltip) | `static/js/campaigns.js` |
| 2 | Newsletter dashboard chart plots `sent` | `static/js/charts.js` |
| 3 | Email/name column suggestion + ragged-row detection & warning banner | `app/services/csv_converter.py`, `static/js/converter.js`, `tests/test_csv_converter.py` |
| 4 | Reply text tidied (`[image: …]`, blank runs) and stored replies re-normalised | `app/services/imap_reply_tracker.py`, `tests/test_fixes_round2.py` |
| 5 | Bounce ingestion: 30-day window, `bounce_processed.json` idempotency, delay-notice filter, inline-DSN recipient parsing | `app/services/bounce_ingest.py`, `tests/test_bounce_ingest.py` (12 new tests) |
| 6 | Deliverability audit: 10 checks, informational checks excluded from the score, failing checks named, honest badge rendering | `app/services/deliverability_service.py`, `static/js/deliverability.js`, `tests/test_replies_deliverability.py` |
| 7 | ListMonk settings writes: merge + de-mask (`apply_settings`), fixing the broken 1-click preset | `app/services/listmonk_client.py`, `app/services/deliverability_service.py`, tests |
| 8 | Sticky table action column + tighter cell padding ≤1440 px | `static/css/style.css` |
| 9 | Visible switch track token (`--toggle-track`) for both themes | `static/css/style.css`, `static/js/unsubscribes.js` (markup unchanged) |
| 10 | `tpl` badge + raw-template tooltip on templated subjects | `static/js/campaigns.js` |
| 11 | Reusable `.notice` warning component | `static/css/style.css` |
| 12 | Asset cache-busting versions bumped | `templates/index.html` |
| 13 | Docs: bounce state, audit scoring, converter suggestions, two new coding rules (settings PUT is all-or-nothing; never trust IMAP `\Seen`) | `AGENTS.md`, `.env.example` |
| 14 | Categorized sidebar navigation (5 groups, mode-aware) | `templates/index.html`, `static/css/style.css`, `static/js/app.js` |
| 15 | Plain-text alt-body field + placeholder hint on campaign create/edit | `static/js/campaigns.js`, `static/css/style.css` |

### Production changes (applied, with backups)

| Change | Before → after | Rollback |
|--------|----------------|----------|
| Removed the Ghost Ink hidden-text block from `sequence_1_template` | 18,074 → 7,343 bytes, 0 `ghost_ink` refs, unsubscribe link + `email_1_body` intact | `/tmp/opencode/sequence_1_template.backup.html` |
| Media upload allow-list | `["jpg","jpeg","png","gif","svg","*"]` → `["jpg","jpeg","png","gif","svg","webp"]` | `/tmp/opencode/settings-backup.json` |
| `.env` permissions | `644` → `600` | — |

The settings change was made **through** the new `apply_settings()` path as a live test of the
fix: the SMTP password mask length was identical before and after (16 chars), confirming the
stored secret was preserved, and `app.batch_size`, `app.concurrency`, tracking/privacy flags
and the bounce block were all unchanged.

### Deliberately left to the operator

These change live sending/consent behaviour and were not applied:

1. **ListMonk bounce mailbox / provider webhook** (audit B2–B4) — the dashboard scanner now
   ingests DSNs reliably, but ListMonk's own `bounce.actions` (hard → blocklist) still cannot
   fire until a mailbox or webhook is configured.
2. **Sending cadence** — `1 msg / 2 min ≈ 720/day`, the most likely reason campaigns left
   1,078 messages queued.
3. ✅ **Done** — newsletter readiness batch applied (see the audit doc section D).
4. **SMTP `from_addresses`** — currently unrestricted; restricting to `@getaevoramedia.com`
   prevents a future sender-address mistake.
5. **Campaign subject / plain-text body** — content decisions for the next sequence.
6. **DMARC `rua=` reporting address** — DNS-side change; add it and plan the move to `p=reject`.