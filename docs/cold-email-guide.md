# ListMonk Pro — Cold Email Outreach Guide

This guide details the architecture, deliverability mechanics, and operational workflow of **Cold Email Mode** in ListMonk Pro.

---

## 1. Overview & Dual-Mode Philosophy

ListMonk is historically built as a high-throughput marketing newsletter engine. However, cold email outreach requires the exact opposite strategy:
- **Newsletter**: High batch sizes (1,000+), tracking pixels enabled, link tracking redirects enabled, RFC `List-Unsubscribe` headers present.
- **Cold Email**: Low batch sizes (100), single worker concurrency, zero tracking pixels, no link redirects, RFC `List-Unsubscribe` one-click headers kept (a Gmail/Yahoo bulk-sender requirement).

ListMonk Pro bridges this gap by introducing **Dual Operating Modes**:
1. **📰 Newsletter Mode**: Standard broadcast marketing with open/click tracking and rich HTML templates.
2. **🎯 Cold Email Mode**: Deliverability-first outreach with IMAP reply tracking, sentiment categorization, and zero-pixel sending.

---

## 2. Deliverability Engine Rules

When Cold Email Mode is active or the **Cold Email Deliverability Preset** is applied, the following settings are enforced via ListMonk API:

### Rule 1: Zero Tracking Pixels (`privacy.disable_tracking: true`)
- **Why**: Spam filters (Google Workspace, Microsoft 365) detect invisible 1x1 GIF pixels (`/display/...`). When detected from a new or un-warmed domain, emails are classified as bulk promotions or spam.
- **Result**: Disabling tracking removes the pixel entirely. The email looks and behaves like an authentic 1-to-1 message sent directly from an email client.

### Rule 2: No Link Redirect Rewrites (`privacy.disable_tracking: true`)
- **Why**: Tracking links replace your original links with `lm.yourdomain.com/r/...` wrappers. Security scanners flag these redirect hops as phishing risks.
- **Result**: Links in cold emails remain clean and direct.

### Rule 3: Keep the RFC List-Unsubscribe Header (`privacy.unsubscribe_header: true`)
- **Why**: Since February 2024 Gmail and Yahoo require one-click unsubscribe (RFC 8058 `List-Unsubscribe` + `List-Unsubscribe-Post`) from anyone sending ~5,000+ messages a day, and enforce it with rejections and spam-foldering. ListMonk only emits the headers with this setting on. Without them, a prospect who wants out has one button left — *Report spam* — and the complaint rate is what actually sinks a cold-outreach domain.
- **Result**: Cold emails carry the one-click header (shown by Gmail as an "Unsubscribe" link next to the sender) *and* an honest, human opt-out line in the body (e.g., *"Reply 'remove me' to opt out"*), which the IMAP unsubscribe monitor honours automatically.

### Rule 4: Conservative Batching & Concurrency (`batch_size: 100`, `concurrency: 1`)
- **Why**: Blasting 1,000 emails per second triggers provider rate limits and spam filters.
- **Result**: Safe batch sizes of 100 with 1 worker thread drip emails in a natural cadence that resembles human sending.

---

## 3. Live IMAP Reply Tracking

In cold email, **Reply Rate** is the primary success metric (not open rate).

### How It Works:
1. **Background Scanner**: A persistent worker connects to your IMAP mailbox (`IMAP_HOST`, port 993 SSL) every 15 minutes.
2. **Quoted Thread Stripping**: The scanner extracts *only* the new reply text from the prospect, stripping all `>` quotes, `On ... wrote:`, and template footers.
3. **Sentiment Classification**:
   - 🟢 **Positive / Hot Lead**: Detects intent phrases like *"sounds good"*, *"let's talk"*, *"schedule a call"*, *"pricing"*, *"send info"*.
   - 🟡 **Question / Inquiries**: Detects questions (*"how does this work"*, *"cost"*, *"details"*).
   - ⚪ **Out-of-Office / Auto-Reply**: Detects vacation and auto-responder notices.
   - 🔴 **Not Interested**: Detects opt-outs (*"unsubscribe"*, *"not interested"*, *"stop"*).
4. **Campaign Attribution**: Matches incoming replies to active ListMonk campaigns by subject line, recipient, or `In-Reply-To` headers.
5. **Feed & Lead Management**:
   - Replies appear in real-time under `#/replies`.
   - 1-click status actions: **Mark as Hot Lead**, **Copy Email**, **Blocklist / Unsubscribe**.

---

## 4. Deliverability Pre-Flight Checklist (`#/deliverability`)

Before launching outreach campaigns, visit the **Deliverability & Safety** page to review your audit score:
- **100% Score**: All cold email parameters are optimal.
- **< 100% Score**: Displays warning badges explaining which setting needs attention.
- **1-Click Sync**: Click **"Apply Cold Email Deliverability Preset"** to immediately sync all engine settings with ListMonk via API.
