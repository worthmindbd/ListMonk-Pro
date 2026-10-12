# ListMonk Settings Audit — essential configuration check

> **Status update (2026-10-12, post-fix).** The audit in the app now covers 10 checks
> (bounce feedback loop, hard-bounce action, send cadence, media upload allow-list added) and
> excludes informational checks from the score. It first reported an honest **77%**; after the
> fixes below the instance genuinely scores **100%** with the sending cadence still surfaced as
> an INFO row (`≈720 msgs/day`).
>
> Fixes applied live (each with a backup): media upload allow-list hardened (`*` removed),
> `bounce.webhooks_enabled` turned **on** — without it ListMonk never registers
> `POST /webhooks/bounce` and every bounce insert 404d — and the settings-write bug that made
> the 1-click preset fail with *"At least one SMTP block should be enabled"* is fixed.
> Items still requiring an operator decision are marked **ACTION** below.

**Date:** 2026-10-12
**Instance:** `https://lm.getaevoramedia.com` (ListMonk with the server-array settings schema, i.e. v4+/v5+ generation)
**Method:** audit via `GET /api/settings`, `/api/lists`, `/api/templates`, `/api/campaigns`, `/api/bounces`, `/api/dashboard/counts`, `/api/health`, plus a direct SMTP STARTTLS probe, read-only IMAP mailbox inspection and public DNS lookups. The original audit changed **nothing**; the three changes listed in the status update above were applied afterwards, each through the app's merge-based `apply_settings()` with the previous value backed up (no subscriber, campaign, template body or list was deleted).
**Reference mode:** the instance is currently tuned for **cold email outreach**. Where a setting is correct for cold email but wrong for newsletter/bulk sending, that is called out explicitly.

---

> **Deployed and verified (2026-10-12, `fb4c643`).** The fix commits went out through the GitHub
> Actions deploy workflow (`git reset --hard` + `docker compose up -d --build` + health check — all
> runs green) and `https://dash.getaevoramedia.com` is serving the new code: the categorized nav,
> the alt-body field and the corrected `SENT` value were all confirmed in the deployed assets.
> Production's background bounce scan built its own processed-ID set alongside the 4 records
> already in ListMonk (no duplicates), and comparing its behaviour with the local run surfaced one
> more defect, now fixed: an unattributable DSN was discarded permanently even when the
> campaign-history fetch itself had failed — exactly what a container-start hiccup looks like.
> `fb4c643` retries those instead of losing them.
>
> Final live state: 4 bounce records · `bounce.webhooks_enabled=true` · uploads
> `jpg/jpeg/png/gif/svg/webp` · public subscription page + double opt-in + ALTCHA on ·
> `sequence_1_template` 7,343 bytes and ghost-free.

## 0. Scoreboard

| Category | Status |
|----------|--------|
| A. Sending / transport (SMTP, identity, throughput) | 🟢 configured · 🟡 one deliberate-looking hard throttle (ACTION: confirm intent) |
| B. Bounce & complaint handling | 🟢 **fixed live** — generic bounce webhook enabled; the dashboard scanner now creates records (4 soft bounces recorded, was 0) |
| C. Tracking, privacy & compliance | 🟡 correct for cold email, non-compliant for bulk/newsletter |
| D. Opt-in, lists & public pages | 🟢 **fixed live** — public subscription form + opt-in page + double opt-in + ALTCHA captcha enabled; public double opt-in list created |
| E. Security & uploads | 🟡 upload allow-list fixed live; `smtp.from_addresses` still unrestricted (ACTION) |
| F. Domain authentication (SPF/DKIM/DMARC/TLS) | 🟢 records present · 🟡 DKIM signing unverified, DMARC has no reporting |
| G. Appearance, maintenance, misc | 🟢 acceptable |
| H. Dashboard ↔ ListMonk integration (.env) | 🟢 working; `.env` permissions fixed |

---

## A. Sending / transport

| # | Setting | Current value | Verdict |
|---|---------|---------------|---------|
| A1 | SMTP server(s) | 1 server, `enabled: true`, `mail.getaevoramedia.com:587` | ✅ |
| A2 | SMTP security | `tls_type: STARTTLS`, `tls_skip_verify: false` | ✅ certificate probed: TLS 1.3, `TLS_AES_128_GCM_SHA256`, CN `mail.getaevoramedia.com`, verification **OK** |
| A3 | SMTP auth | `auth_protocol: plain`, user `kawsar@getaevoramedia.com` | ✅ |
| A4 | Connection behaviour | `max_conns: 2`, `max_msg_retries: 2`, `msg_retry_delay: 5s`, `idle_timeout: 15s`, `wait_timeout: 5s` | ✅ conservative/healthy |
| A5 | Sender identity | `app.from_email = Kawsar Mahmud <kawsar@getaevoramedia.com>` — same domain as the SMTP server | ✅ |
| A6 | **Sending throughput** | `app.message_rate = 1` (= 1 msg/sec, not the binding limit) **plus** `app.message_sliding_window = true` with `rate = 1` per `duration = 2m` | 🟡 ListMonk's `pipe.pushBatch` sleeps for the remainder of the window every time `slidingCount >= SlidingWindowRate`, so **rate 1 per 2 m = 1 message every 2 minutes ≈ 720/day**. Confirmed on the VPS: ListMonk's log is full of `pipe.go:126: messages exceeded (1) for the window (2m0s) … Sleeping for 2m0s`, i.e. exactly one message every two minutes. |
| A7 | Batch / workers | `app.batch_size = 100`, `app.concurrency = 1` | ✅ matches the cold-email preset |
| A8 | Error tolerance | `app.max_send_errors = 10` | ✅ |
| A9 | From-address restriction :warning: **ACTION** | `smtp[0].from_addresses = []` (any From allowed), `smtp[0].name = ""` | 🟡 restrict to `@getaevoramedia.com` and name the server |
| A10 | Root URL | `app.root_url = https://lm.getaevoramedia.com` ✔ reachable | ✅ (all unsubscribe / preference links are built from it) |
| A11 | Notifications | `app.notify_emails = ["admin@worthmind.net"]` | ✅ |
| A12 | `app.site_name`, `app.lang` | `Aevora Media`, `en` | ✅ |
| A13 | Update check | `app.check_updates = true` | ✅ |
| A14 | `app.enable_public_archive` / `_rss_content` | `false` / `false` | ✅ for cold email |

## B. Bounce & complaint handling — 🟢 resolved in the fix pass

| # | Setting | Current value | Verdict |
|---|---------|---------------|---------|
| B1 | `bounce.enabled` | `true` | ✅ now meaningful — the generic webhook is on and the scanner feeds it |
| B2 | `bounce.mailboxes` | one entry, `enabled: false`, placeholder values | ℹ️ still a placeholder, but no longer required: the dashboard scanner feeds ListMonk through the generic bounce webhook instead |
| B3 | Provider integrations | ✅ `bounce.webhooks_enabled` **enabled live** (was `false`); provider-specific integrations stay off | 🔴→🟢 ListMonk only registers `POST /webhooks/bounce` when this flag is on — with it off, every `create_bounce` call returned **404** and the app's bounce pipeline could not work at all (`errors: 4`). After enabling: `ingested: 4, errors: 0` |
| B4 | `bounce.actions` | `hard → blocklist (count 1)`, `complaint → blocklist (1)`, `soft → none` | ✅ policy is correct **and now armed** (hard bounces recorded via the webhook trigger it) |
| B5 | Recorded bounces | ✅ **4 records** created from live DSNs (campaign-attributed, `source=api`, smtp_code in meta); 0 hard so far — `bounce.actions` (hard → blocklist) is now wired and will fire on the first genuine hard bounce | 🟢 |
| B6 | Dashboard bounce scanner | ✅ **fixed** — scans a 30-day window and keeps `bounce_processed.json`; delay notices are skipped | verified on the live mailbox: **6 DSNs are now examined where the old UNSEEN-only query saw 0** (5 of them had already been read in webmail) |
| B7 | Blocklisted subscribers | 3 — all with `created_at == updated_at` (2026-10-06T16:48:04Z), i.e. blocklisted during import, not by bounce processing | ℹ️ expected from a suppression list |
| B8 | Live DSN audit (informational) | 6 DSNs in the 30-day window: 2 × `Action: delayed` (correctly ignored) + 4 × `failed` (`5.0.0` "unauthenticated … hosted tenant with no mail-enabled subscription" and `4.4.4`) | 🟡 those 4 are classified **soft** per policy (never blocklist on ambiguous codes). The `5.0.0` text indicates the *recipient's* domain has no mail-enabled subscription — undeliverable in practice, so consider pruning such domains at the source |

**Upstream quirk worth knowing:** `makeOptinNotifyHook` receives `app.send_optin_confirmation` as its `unsubHeader` argument (`cmd/main.go:184`), so the opt-in confirmation mail carries `List-Unsubscribe` headers whenever double opt-in is enabled — independent of `privacy.unsubscribe_header`. Harmless (arguably correct for a subscription notification) but surprising.

**Resolved:** the dashboard owns bounce ingestion (mailbox scan + classification) and feeds ListMonk through the generic bounce webhook; ListMonk owns the action policy (hard → blocklist). Both halves are now working — verified end to end with 4 live DSNs. The only reason no *hard* bounce exists yet is that every observed failure was a 5.0.0/4.4.4 policy rejection, which is deliberately classified soft.

## C. Tracking, privacy & compliance

| # | Setting | Current value | Verdict |
|---|---------|---------------|---------|
| C1 | `privacy.disable_tracking` | `true` | ✅ required for cold email (no pixel, no link rewrite) |
| C2 | `privacy.individual_tracking` | `false` | ✅ |
| C3 | `privacy.unsubscribe_header` | `false` | ✅ for 1:1 cold email (avoids the Promotions tab) · 🔴 **not acceptable for bulk/newsletter sends** — Gmail and Yahoo require one-click `List-Unsubscribe` (and `List-Unsubscribe-Post`) from bulk senders (~5,000+/day), and ListMonk only attaches those headers when this setting is `true` (`manager.worker`: `if m.cfg.UnsubHeader { … }`). A body link is still present in the template (`{{ UnsubscribeURL }}`, plus a `?manage=true` preference link), so opt-out is possible — but the header requirement is unmet |
| C4 | `privacy.allow_blocklist / export / preferences / wipe` | all `true` | 🟡 acceptable; `allow_wipe=true` lets visitors erase their own record once public pages are on |
| C5 | `privacy.record_optin_ip` | `false` | ✅ privacy-friendly |
| C6 | `privacy.exportable` | `profile, subscriptions, campaign_views, link_clicks` | ✅ |
| C7 | `privacy.domain_allowlist` / `domain_blocklist` | both empty | 🟡 consider a domain blocklist as an abuse guard |
| C8 | Preset coupling | the app's `apply-preset` writes **global** settings, so switching the dashboard to Newsletter mode flips C1–C3 for cold-email campaigns too | 🟡 documented in `AGENTS.md` (rule 12); note the endpoint was also **broken** before the fix pass — a partial settings PUT is rejected by ListMonk (`At least one SMTP block should be enabled`) and would have overwritten the SMTP password with the `••••` mask. Now merged via `apply_settings()` |

## D. Opt-in, lists & public pages

| # | Setting | Current value | Verdict |
|---|---------|---------------|---------|
| D1 | `app.enable_public_subscription_page` | ✅ **enabled live** — `/subscription/form` now returns 200 | 🟢 self-serve opt-in path exists; the two private cold-email lists stay hidden from it (`/api/public/lists` returns only the newsletter list) |
| D2 | `app.show_optin_page` | ✅ **enabled live** | 🟢 |
| D3 | `app.send_optin_confirmation` | ✅ **enabled live** | 🟢 double opt-in active for `optin=double` lists |
| D4 | Default transactional template | ✅ **no longer a gap — the original finding was wrong.** ListMonk's opt-in mail uses the built-in system template `notifs.TplSubscriberOptin` (`cmd/subscribers.go: makeOptinNotifyHook`), **not** a row from the templates table. `tx` templates are only used by `POST /api/tx` with an explicit `template_id` | ℹ️ Note: `PUT /api/templates/{id}/default` only sets a **campaign** template (`queries.sql: set-default-template` sets `is_default=true WHERE id=$1 AND type='campaign'` and clears every other template). Calling it for a `tx`/`campaign_visual` id silently clears the campaign default — it was called and reverted during this pass |
| D5 | Archival template | *“Default archive template”* exists, not marked default (`archive_template_id: null` on campaigns) | ℹ️ moot while `enable_public_archive=false` |
| D6 | Lists | ✅ 3 lists now — existing `Default list` (1,931, private, single) and `Default list (Remaining Leads)` (996, private, single) untouched, plus new **`Aevora Newsletter`** (public, **double** opt-in, 0 subs) as the public signup destination | 🟢 cold-email lists are not selectable on the public form |
| D7 | `security.captcha` | ✅ ALTCHA **enabled live** (`hcaptcha` still off — ALTCHA needs no third-party keys and is served from the instance itself: `/public/static/altcha.umd.js`) | 🟢 public form is captcha-protected |

## E. Security & uploads

| # | Setting | Current value | Verdict |
|---|---------|---------------|---------|
| E1 | `security.oidc` | disabled | ✅ (single admin login) |
| E2 | `security.trusted_urls` | `[]` — relies on `root_url` | ✅ |
| E3 | `upload.provider` | `filesystem` → `uploads` served at `/uploads` (directory listing disabled, 404) | ✅ |
| E4 | `upload.extensions` | ✅ **fixed live** — `["jpg","jpeg","png","gif","svg","webp"]` (was `… ,"*"`) | the `*` wildcard allowed `.html`/`.js` uploads into a publicly served path (stored-XSS / content-hosting). Applied through the app's new `apply_settings()` merge; old value backed up to `/tmp/opencode/settings-backup.json` |
| E5 | ListMonk API auth | API key works; `/api/health` without credentials returns `403 {"message":"invalid session"}` | ℹ️ external uptime monitors need credentials |
| E6 | Admin surface | `/admin/` → 307 to `/admin/login`; `/` → public ListMonk landing page (`<title>listmonk - Aevora Media</title>`) | 🟡 consider whether the public root page should be exposed while public pages are disabled |

## F. Domain authentication (verified by DNS)

| # | Record | Value | Verdict |
|---|--------|-------|---------|
| F1 | MX | `mail.getaevoramedia.com` | ✅ |
| F2 | SPF | `v=spf1 a mx ip4:173.249.56.172 -all` | ✅ hard fail, sending IP covered (`lm.` resolves to the same IP) |
| F3 | DKIM | ✅ **verified on the VPS (was "unverified")** — exim signs with `dkim_selector = mail`, `dkim_canon = relaxed`, `dkim_strict = 0` (`/etc/exim4/exim4.conf.template:404`) using the per-domain key `/etc/exim4/domains/getaevoramedia.com/dkim.pem`. The public key derived from that **signing key** has the same SHA-256 fingerprint as the published `mail._domainkey` record (`6e6eaef16526340827c2ffd4ba4eecff53cf804bed50ba57c41d81d2bc587d07`), so signing and DNS are aligned |
| F4 | DMARC :warning: **ACTION** | `v=DMARC1; p=quarantine; pct=100` | 🟡 no `rua=`/`ruf=` — you get no failure reports. Add `rua` and plan the move to `p=reject` |
| F5 | TLS on the ListMonk UI | nginx, HTTPS | ✅ |
| F6 | TLS on SMTP submission | STARTTLS, valid cert, TLS 1.3 | ✅ (probed directly) |

## G. Appearance, maintenance, misc

| # | Setting | Current value | Verdict |
|---|---------|---------------|---------|
| G1 | `appearance.*.custom_css/js` | all empty | ✅ clean |
| G2 | `app.logo_url`, `app.favicon_url` | empty | 🟡 cosmetics — set the Aevora logo/favicon (surfaced on public pages/unsub pages) |
| G3 | `app.cache_slow_queries` | `false` (interval `0 3 * * *` unused) | 🟡 enable if the dashboard feels slow on large lists |
| G4 | `maintenance.db.vacuum` | `vacuum: false`, cron `0 2 * * *` | 🟡 consider `vacuum: true` for Postgres bloat over time |
| G5 | `upload.filesystem.upload_uri` | `/uploads` | ✅ public media reachable |

## H. Dashboard ↔ ListMonk integration (`.env`)

| # | Item | Verdict |
|---|------|---------|
| H1 | `LISTMONK_URL` / `LISTMONK_USER` / API key | ✅ API responds; connection status shows *Connected* |
| H2 | `DASHBOARD_USER` / `DASHBOARD_PASS` | ✅ login works, non-default credentials, rate-limited |
| H3 | `SESSION_SECRET` | ✅ explicitly set in `.env` (64-char hex), so sessions survive restarts; note a leftover `session_secret.key` (mode 600) from an earlier auto-generated run also exists — `_load_or_create_secret_key()` prefers the env value, so the file is inert. In Docker the data dir is the `listmonk-pro-data` volume |
| H4 | `IMAP_*` (reply tracker + unsubscribes) | ✅ `IMAP CONNECTED`; mailbox `info@getaevoramedia.com` reachable |
| H5 | `BOUNCE_IMAP_*` | ✅ reachable (2 messages) but see B6 |
| H6 | `TRUST_PROXY` | unset — fine for the same-host/private-peer Nginx setup described in `.env.example` |
| H7 | **File permissions** | ✅ **fixed locally and on the VPS** — `/root/ListMonk-Pro/.env` was `644` root:root and is now `600`. `bounce_processed.json` (created by the new scanner) was the one state file that came out `644`; the writer now uses an atomic 0600 write like its siblings, and the deployed file was corrected |

---

## Recommended fix order

1. ✅ **Done — bounce ownership** (B1–B6): scanner fixed, generic bounce webhook enabled, 4 live DSNs ingested, `bounce.actions` armed.
2. ✅ **Done — hidden-text block** (project review item 3): removed from `sequence_1_template`; every email had been carrying ~1,609 words of invisible text.
3. ✅ **Done — `SENT` mislabel** (items 1–2) and the settings-write bug that broke the 1-click preset (item 7).
4. ✅ **Done — hardening:** `upload.extensions` wildcard removed, `.env` set to `600`.
5. ✅ **Answered — the 1,078 unsent messages.** Read from the VPS:
   `docker logs listmonk_app` (ListMonk **v6.2.0**) contains **zero** `error sending message in
   campaign` lines and **zero** `stop processing campaign` lines, but each campaign logs
   `start processing campaign` **twice** (Oct 7 16:21→16:23 and Oct 9 17:51→18:51) — a pipe
   restart mid-queue. The DB shows `sent < to_send` with `last_subscriber_id = max_subscriber_id
   = 5801` for both campaigns, so ListMonk believed the subscriber range was exhausted and
   closed them as `finished`. The MTA agrees with ListMonk, not with `to_send`: exim accepted
   737 messages in campaign #3's window and 798 in campaign #4's (from `kawsar@`), i.e. the
   remainder was never handed over — nothing is stuck (queue is empty today). Root cause is the
   combination of a ~31-hour queue drain with an interruption: when the pipe restarted, the
   progress marker had already advanced to the end of the last processed batch, so the rest of
   the audience was silently skipped.
   **Recommendation:** keep the 720/day pace (a sound single-domain volume) but stop running a
   single campaign for days — either raise the effective rate so a send completes in one
   window, or split the audience into daily batches (which is effectively what the
   `Remaining Leads` list did). Keep an eye on `sent` vs `to_send` after every send: the
   dashboard now reports the real number, and the audit surfaces the cadence.
6. ✅ **Done — newsletter readiness:** public subscription page, opt-in page, double opt-in and ALTCHA captcha enabled; public double opt-in list `Aevora Newsletter` created. Remember the newsletter preset still turns `privacy.unsubscribe_header` back on globally, and add a `rua=` DMARC address when convenient (DNS-side).
7. **ACTION — optional:** restrict `smtp[0].from_addresses` to `@getaevoramedia.com`, and add a plain-text alt body / per-lead subject to the next campaign.
8. **Verify with one test send** (to a Gmail address): DKIM signature present, unsubscribe link resolves, and placement is Primary rather than Spam — the only remaining checks that cannot be done through the API.

---

## Items that need a test send to confirm

| Item | How to confirm |
|------|----------------|
| DKIM signing active (F3) | Send one email and inspect `Authentication-Results` / “signed-by” |
| Unsubscribe URL end-to-end | Send a test to a real inbox and click `{{ UnsubscribeURL }}` (not done here — it would have unsubscribed a live recipient) |
| Inbox placement vs Spam/Promotions | Send to a Gmail + Outlook seed address and check folder placement |
| MTA throttling / queue behaviour (A6) | Inspect the mail-server queue log while a batch is running |