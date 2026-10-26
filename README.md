# ListMonk Pro

A modern, high-performance management dashboard and automation suite for your self-hosted [ListMonk](https://listmonk.app/) instance. Built with FastAPI and vanilla JavaScript, with dedicated dual operating modes: **Newsletter** and **Cold Email**.

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![FastAPI](https://img.shields.io/badge/FastAPI-0.141-green) ![Docker](https://img.shields.io/badge/Docker-Ready-blue) ![License](https://img.shields.io/badge/License-MIT-yellow)

---

## ⚡ Dual Operating Modes

ListMonk Pro adapts instantly between two distinct marketing disciplines with a single click in the top navigation or on first login:

### 1. 📰 Newsletter Mode
- **Designed for:** Audience broadcasts, publishers, product updates, and content creators.
- **Key Features:**
  - Open and click tracking with real-time visual charts.
  - Full HTML rich template designer and multi-device previewer.
  - Public subscription pages and opt-in lists.
  - Link unsubscribe scanner and automated engagement analytics.

### 2. 🎯 Cold Email Mode
- **Designed for:** High-deliverability B2B sales outreach and lead prospecting.
- **Key Features:**
  - **Live IMAP Reply Tracker & Inbox Feed** — Scans your mailbox for incoming replies, strips quoted threads, and categorizes sentiment (🟢 Positive / Hot Lead, 🟡 Questions, ⚪ Out-of-Office, 🔴 Not Interested).
  - **Zero-Pixel Deliverability Safeguard** — Automatically disables tracking pixels and link rewrite redirects to make cold emails indistinguishable from personal 1-to-1 messages.
  - **One-Click List-Unsubscribe** — Keeps the RFC 8058 `List-Unsubscribe` header Gmail and Yahoo require from bulk senders, so prospects who want out unsubscribe instead of reporting spam.
  - **Pre-Flight Deliverability Audit & Presets** — 1-click preset switching (`Cold Email` vs `Newsletter`) with real-time configuration scores.
  - **Lead Management** — Automatically labels subscribers as Leads and lists as Lead Lists, with 1-click status flagging (Hot Lead, Replied, Archived).

---

## Features

### Core Outreach & Management
- **Replies & Prospect Feed** — Real-time monitoring of prospect responses, sentiment filtering, search, and one-click lead tagging.
- **Deliverability & Safety** — Pre-flight audit checking tracking pixels, the List-Unsubscribe header, batch size, worker concurrency, and public archive/subscription exposure.
- **Subscribers & Leads** — Search, filter by list, create, edit, delete, blocklist, bulk blocklist, and bulk delete.
- **Lists & Lead Lists** — Create and manage mailing and lead lists with tags and single/double opt-in controls.
- **Campaigns** — Full campaign lifecycle management: create, edit, start, pause, resume, cancel, archive, delete, HTML/text preview, and test sends.
- **Templates** — Create, edit, and preview templates, with one-click default template assignment.
- **Bounces** — View and filter bounces by campaign or type (`hard`/`soft`), delete records, and export CSVs.
- **Unsubscribes** — Unified dashboard tracking both IMAP reply unsubscribes and direct link clicks with campaign grouping, per-campaign CSV export, and undo/reset capabilities.

### Analytics & Reporting
- **Overview Dashboard** — Summary metrics (subscribers, lists, campaigns) and interactive Chart.js visualizations.
- **Campaign Analytics** — Views, clicks, bounces, and link tracking over time with custom date range filtering.
- **Subscriber-Level Engagement Exports** — Download CSV exports of the exact subscribers who opened, clicked, or hard-bounced any campaign.
- **Campaign Comparison** — Side-by-side performance comparison charts across all campaigns.
- **Summary Exports** — One-click CSV export of all campaigns or raw analytics time-series data.

### Smart Bounce Handling & Ingestion
- **RFC 3463 DSN Classifier** — Accurately parses bounce DSNs into **hard** (invalid address, no such user, mailbox disabled) vs **soft** (mailbox full, greylist, reputation, rate limits).
- **Opener False-Positive Filtering** — Recipients who actually opened a campaign (`campaign_views`) are automatically excluded from hard bounce lists, metrics, and exports.
- **Hard Bounce Caching** — True hard bounce counts are calculated and cached in memory (refreshed every 5 minutes in the background), keeping campaign listing fast and accurate.
- **Automated IMAP Ingestion** — Scans your bounce mailbox every hour for bounce notifications not ingested yet (read or unread), attributes them to the correct campaign via headers or lists, and creates matching records in ListMonk.

### Dual-Source Unsubscribe Engine
- **IMAP Reply Monitor** — Scans your inbox hourly for unsubscribe keywords (`"Remove me"`, `"Unsubscribe me"`, `"Exclude me"`).
- **Quote-Stripping Filter** — Strips quoted reply history and template headers so footer text (e.g. *"Reply with 'Remove me'"*) never triggers false positives.
- **Link Unsubscribe Scanner** — Polls ListMonk lists for subscribers who clicked the direct unsubscribe link.
- **List-Aware Campaign Attribution** — Attributes unsubscribes to campaigns that actually targeted the subscriber's lists.
- **Automated Actions** — Automatically removes the contact from all lists and optionally blocklists them (toggleable in Settings).
- **Undo / Reset Flow** — Restore unsubscribed contacts back to their original lists, re-enable their status, and clear restored log entries in one click.

### CSV Converter & Direct Importer
ListMonk requires a specific CSV format (`email`, `name`, `attributes` as JSON). This built-in tool handles arbitrary CSV files:
1. **Upload** any CSV, TSV, or delimited text file (delimiter and encoding are auto-detected).
2. **Auto-Detect & Map** columns visually: email and name are pre-selected from the headers or sample values, and every remaining column is preserved automatically as a JSON attribute (or hand-pick specific columns instead).
3. **Preview** the exact rows ListMonk will receive, including skipped rows with malformed or missing email addresses.
4. **Download** the converted CSV or **import directly** into ListMonk with selected target lists and subscription mode (`subscribe` or `blocklist`).

### Settings & Automation
- **Campaign Scheduler** — Enforces daily sending windows (e.g., 8:00 AM – 8:00 PM EST) and allowed days (Mon–Fri, weekends). Running campaigns outside the window auto-pause, and auto-paused campaigns automatically resume when the window opens.
- **Auto-Unblock Protection** — Identifies engaged subscribers (who clicked links or opened campaigns) who were accidentally blocklisted by false bounces, re-enables them, and purges their bounce records. Only bounce-driven blocklistings are reversed: anyone who unsubscribed, and blocklists you set yourself, stay blocked. Runs every 6 hours or manually.
- **Persistent Data Store** — Runtime configuration and logs persist to `DATA_DIR` (backed by a named volume in Docker).

### Security
- HMAC-SHA256 session-based cookie authentication with automatic secret generation.
- All API endpoints and pages protected by authentication middleware.
- 7-day session lifetime with clean login/logout flow.

---

## Quick Start

### Option 1: Docker (Recommended)

```bash
git clone https://github.com/worthmindbd/ListMonk-Pro.git
cd ListMonk-Pro
cp .env.example .env
nano .env  # Configure your credentials
docker compose up -d --build
```

Open **http://localhost:8000** and log in.

### Option 2: Manual / Local Development

```bash
git clone https://github.com/worthmindbd/ListMonk-Pro.git
cd ListMonk-Pro
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env  # Configure your credentials
./start.sh
# Or manually:
# uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Configuration

Configure your environment in `.env` (or copy `.env.example`):

```env
# ListMonk API Connection
LISTMONK_URL=https://your-listmonk-instance.com
LISTMONK_USER=listmonk
LISTMONK_API_KEY=your-api-key-here

# Dashboard Login Credentials (MUST be changed from defaults)
DASHBOARD_USER=admin
DASHBOARD_PASS=changeme

# Session Signing Key (leave empty to auto-generate and persist to DATA_DIR,
# or set a random 64-char hex string for persistent sessions across restarts)
SESSION_SECRET=

# Set to true when the app ALWAYS sits behind a trusted reverse proxy, so that
# X-Forwarded-For is honoured for login rate limiting. Leave unset to only
# trust loopback/private peers (the usual same-host Nginx setup).
# TRUST_PROXY=true

# Log level for the app's own loggers (default INFO).
# LOG_LEVEL=INFO

# IMAP Settings for Unsubscribe Monitoring (Optional)
IMAP_HOST=mail.example.com
IMAP_PORT=993
IMAP_USER=your@email.com
IMAP_PASS=your-password
IMAP_USE_SSL=true

# IMAP Settings for Bounce Mailbox Monitoring (Optional)
BOUNCE_IMAP_HOST=mail.example.com
BOUNCE_IMAP_PORT=993
BOUNCE_IMAP_USER=bounces@yourdomain.com
BOUNCE_IMAP_PASS=your-password
BOUNCE_IMAP_USE_SSL=true

# Persistent Data Storage Directory (Optional: defaults to /data in Docker, repo root in local dev)
# DATA_DIR=/data
```

Generate a random session secret:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## Production Deployment (VPS with Nginx)

### 1. Clone & Configure
```bash
cd /opt
git clone https://github.com/worthmindbd/ListMonk-Pro.git
cd ListMonk-Pro
cp .env.example .env
nano .env  # Set your credentials and secure passwords
```

### 2. Start with Docker Compose
```bash
docker compose up -d --build
```

### 3. Nginx Reverse Proxy Configuration
```nginx
server {
    listen 80;
    server_name dash.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### 4. SSL with Let's Encrypt / Certbot
```bash
sudo ln -s /etc/nginx/sites-available/listmonk-dashboard /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d dash.yourdomain.com
```

---

## Background Tasks

## Background Tasks

The dashboard runs six background loops started during application lifecycle:

| Background Task | Frequency | Description |
|-----------------|-----------|-------------|
| **Campaign Scheduler** | Every 60s | Checks running campaigns and pauses/resumes them based on the send window |
| **Hard Bounce Cache** | On startup + every 5 min | Computes true hard bounce counts per campaign, excluding openers |
| **Prospect Reply Tracker** | Every 15 min | Scans IMAP inbox for cold email prospect replies and sentiment classification |
| **IMAP & Link Unsubscribe Scan** | Every 1 hour | Scans inbox for reply keywords and polls ListMonk for link unsubscribes |
| **Bounce Ingestion** | Every 1 hour | Ingests and classifies new DSNs (last 30 days, read or unread) from the bounce IMAP mailbox |
| **Auto-Unblock Protection** | Every 6 hours | Scans for engaged contacts blocklisted by bounces, re-enables them, and purges bounce records (opt-outs are never re-enabled) |

---

## Project Structure

```
ListMonk-Pro/
  app/
    main.py                    # FastAPI application, auth middleware, background task lifespan
    auth.py                    # Session cookie creation, signing, and verification
    config.py                  # Settings loader and DATA_DIR path resolver
    routers/
      subscribers.py           # Subscriber & Lead CRUD, import status, bulk actions, exports
      lists.py                 # List & Lead List CRUD
      campaigns.py             # Campaign & Cold Outreach CRUD, running stats, analytics, exports
      templates.py             # Template CRUD & default template selection
      bounces.py               # Bounce management, ingestion, filtered CSV exports, bulk deletions
      converter.py             # CSV detection, conversion, and direct ListMonk import
      unsubscribes.py          # Unsubscribe dashboard, campaign groups, manual scans, undo/reset
      replies.py               # Prospect reply feed, sentiment filters, hot lead status updates
      deliverability.py        # Deliverability pre-flight audit checklist & 1-click presets
    services/
      listmonk_client.py       # Async HTTP client for ListMonk REST API (with paginate_all helper)
      csv_converter.py         # CSV converter transforming arbitrary columns to ListMonk schema
      export_service.py        # Memory-efficient streaming CSV generator
      auto_unblock.py          # Auto-unblock bounce-blocklisted engaged contacts (never opt-outs)
      campaign_scheduler.py    # Timezone-aware send window scheduler
      deliverability_service.py# Deliverability audit engine and 1-click preset sync
      bounce_ingest.py         # RFC 3463 DSN classifier & IMAP bounce mailbox scanner
      bounce_filters.py        # Opener false-positive bounce filtering
      bounce_list.py           # Fast-path paginated bounce listing with opener exclusion
      hard_bounce_cache.py     # In-memory hard bounce counts cache per campaign
      opener_cache.py          # In-memory cache of campaign opener email sets with TTL
      imap_reply_tracker.py    # IMAP reply monitor, sentiment analysis & quote-thread stripping
      reply_log.py             # Thread-safe persistent JSON storage for prospect replies
      imap_unsubscribe.py      # IMAP reply scanner for unsubscribe keywords with quote stripping
      link_unsubscribe.py      # ListMonk direct link unsubscribe scanner
      imap_helpers.py          # Shared IMAP & string escaping utilities
      unsubscribe_log.py       # Thread-safe persistent JSON state manager
      task_utils.py            # Fire-and-forget task references & shutdown drain
  static/
    css/style.css              # Dark theme stylesheet with mode toggle & reply feed cards
    js/
      api.js                   # Fetch API wrapper with 401 handling
      app.js                   # Single-Page Application router, mode switcher & onboarding
      analytics.js             # Campaign analytics charts and link click tracking
      campaigns.js             # Campaign management, status transitions, test sends
      charts.js                # Mode-aware dashboard overview charts & summary cards
      converter.js             # CSV converter UI, column mapping, and import modal
      deliverability.js        # Deliverability pre-flight audit checklist & 1-click presets
      lists.js                 # Mailing list management
      replies.js               # Prospect reply feed, sentiment badges, hot lead flagging
      settings.js              # Settings UI (scheduler, auto-unblock, unsubscribe toggles)
      subscribers.js           # Subscriber management, search, edit modal, bulk actions
      unsubscribes.js          # Campaign-grouped unsubscribe view, source badges, undo/reset
      vendor/chart.umd.min.js  # Vendored Chart.js 4.5.1 (no CDN dependency)
    favicon.png
    favicon.svg
  templates/
    index.html                 # Authenticated SPA shell
    login.html                 # Dark-themed login page
  tests/
    test_analytics.py          # Analytics endpoint & export tests
    test_bounce_classify.py    # RFC 3463 bounce classifier tests
    test_bounce_filters.py     # Opener exclusion & false positive filter tests
    test_link_unsubscribe.py   # Link unsubscribe scanner & dedup tests
    test_regressions.py        # Route order, IMAP seen-marking, prune, timestamp regressions
  .github/workflows/
    deploy.yml                 # Automated SSH deploy on push to main
  docker-compose.yml           # Compose definition
  Dockerfile
  pytest.ini
  requirements.txt
  requirements-dev.txt
  start.sh
  .env.example
```

---

## API Reference

Once running, visit **http://localhost:8000/docs** for the interactive Swagger UI (requires login).

### Key Endpoints

| Category | Endpoint | Method | Description |
|----------|----------|--------|-------------|
| **Auth** | `/auth/login` | GET / POST | Login page / authenticate session |
| | `/auth/logout` | GET | End session & redirect |
| **Subscribers** | `GET /api/subscribers` | GET | List subscribers with search/filter |
| | `GET /api/subscribers/export-all` | GET | Export all subscribers as CSV |
| | `GET /api/subscribers/import/status` | GET | Check subscriber import status |
| | `GET /api/subscribers/import/logs` | GET | View subscriber import logs |
| | `GET /api/subscribers/{id}` | GET | Get subscriber details |
| | `GET /api/subscribers/{id}/export` | GET | Export subscriber profile JSON |
| | `GET /api/subscribers/{id}/bounces` | GET | Get bounces for subscriber |
| | `POST /api/subscribers` | POST | Create subscriber |
| | `PUT /api/subscribers/{id}` | PUT | Update subscriber |
| | `PUT /api/subscribers/{id}/blocklist` | PUT | Blocklist single subscriber |
| | `PUT /api/subscribers/blocklist` | PUT | Bulk blocklist subscribers by ID array |
| | `PUT /api/subscribers/lists` | PUT | Bulk modify list memberships |
| | `DELETE /api/subscribers/{id}` | DELETE | Delete single subscriber |
| | `DELETE /api/subscribers` | DELETE | Bulk delete subscribers |
| **Lists** | `GET /api/lists` | GET | List all mailing lists |
| | `POST /api/lists` | POST | Create mailing list |
| | `GET /api/lists/{id}` | GET | Get mailing list details |
| | `PUT /api/lists/{id}` | PUT | Update mailing list |
| | `DELETE /api/lists/{id}` | DELETE | Delete mailing list |
| **Campaigns** | `GET /api/campaigns` | GET | List all campaigns (with cached hard bounce count) |
| | `GET /api/campaigns/running/stats` | GET | Real-time stats for running campaigns |
| | `GET /api/campaigns/summary` | GET | Aggregate totals across all campaigns (cached 60s) |
| | `GET /api/campaigns/export-all` | GET | Export all campaigns summary as CSV |
| | `GET /api/campaigns/analytics/{type}` | GET | Campaign analytics (`views`, `clicks`, `bounces`, `links`) |
| | `GET /api/campaigns/analytics/{type}/export` | GET | Export campaign analytics as CSV |
| | `GET /api/campaigns/{id}` | GET | Get campaign details |
| | `GET /api/campaigns/{id}/preview` | GET | Preview campaign HTML rendering |
| | `GET /api/campaigns/{id}/subscribers/{type}` | GET | View who opened (`views`), clicked (`clicks`), or hard-bounced |
| | `GET /api/campaigns/{id}/subscribers/{type}/export` | GET | Export campaign viewers, clickers, or hard bounces as CSV |
| | `POST /api/campaigns` | POST | Create campaign |
| | `POST /api/campaigns/{id}/test` | POST | Send test email |
| | `PUT /api/campaigns/{id}` | PUT | Update campaign |
| | `PUT /api/campaigns/{id}/status` | PUT | Change status (`start`, `pause`, `cancel`) |
| | `PUT /api/campaigns/{id}/archive` | PUT | Archive campaign |
| | `DELETE /api/campaigns/{id}` | DELETE | Delete campaign |
| **Templates** | `GET /api/templates` | GET | List templates |
| | `POST /api/templates` | POST | Create template |
| | `GET /api/templates/{id}` | GET | Get template details |
| | `PUT /api/templates/{id}` | PUT | Update template |
| | `PUT /api/templates/{id}/default` | PUT | Set default template |
| | `DELETE /api/templates/{id}` | DELETE | Delete template |
| **Bounces** | `GET /api/bounces` | GET | List bounces (filtered by campaign or type) |
| | `POST /api/bounces/ingest` | POST | Manually trigger bounce mailbox IMAP ingestion |
| | `GET /api/bounces/export` | GET | Export bounce records as CSV |
| | `DELETE /api/bounces/{id}` | DELETE | Delete single bounce record |
| | `DELETE /api/bounces` | DELETE | Delete all bounces or filter-matched bounces |
| **CSV Converter** | `POST /api/converter/detect-columns` | POST | Upload CSV and detect columns & preview |
| | `POST /api/converter/preview` | POST | Convert and return the first rows as JSON (exact ListMonk output preview) |
| | `POST /api/converter/convert` | POST | Convert CSV to ListMonk schema (download) |
| | `POST /api/converter/convert-and-import` | POST | Convert and import directly into ListMonk lists |
| **Scheduler** | `GET /api/scheduler` | GET | Get send window schedule configuration & status |
| | `PUT /api/scheduler` | PUT | Update send window schedule settings |
| | `POST /api/scheduler/run` | POST | Manually trigger scheduler evaluation tick |
| **Auto-Unblock** | `GET /api/auto-unblock/status` | GET | Get count of blocklisted engaged contacts |
| | `POST /api/auto-unblock/run` | POST | Manually run auto-unblock process |
| **Unsubscribes** | `GET /api/unsubscribes` | GET | Paginated unsubscribe log |
| | `GET /api/unsubscribes/stats` | GET | Aggregate unsubscribe stats & source breakdown |
| | `GET /api/unsubscribes/settings` | GET | Get unsubscribe settings (e.g. blocklist toggle) |
| | `PUT /api/unsubscribes/settings` | PUT | Update unsubscribe settings |
| | `GET /api/unsubscribes/campaigns` | GET | Campaign-grouped unsubscribe summary |
| | `GET /api/unsubscribes/campaign/{id}` | GET | Records for a specific campaign |
| | `GET /api/unsubscribes/campaign/{id}/export` | GET | Export campaign unsubscribes as CSV |
| | `DELETE /api/unsubscribes/campaign/{id}` | DELETE | Delete unsubscribe records for a campaign |
| | `DELETE /api/unsubscribes/records` | DELETE | Delete specific records by email |
| | `GET /api/unsubscribes/export` | GET | Export all unsubscribe records as CSV |
| | `POST /api/unsubscribes/scan` | POST | Trigger combined IMAP + link unsubscribe scan |
| | `GET /api/unsubscribes/imap-status` | GET | Check IMAP connection health |
| | `DELETE /api/unsubscribes/clear` | DELETE | Clear all unsubscribe log records |
| | `POST /api/unsubscribes/reset` | POST | Undo/reset all unsubscribes & restore memberships |

---

## Testing

Run the test suite with Pytest:

```bash
# Activate virtual environment
source venv/bin/activate

# Run all unit and integration tests
pytest -v
```

---

## Requirements & Prerequisites

- **Python 3.10+** (or Docker)
- A running **ListMonk** instance with REST API access
- **Individual Subscriber Tracking** must be enabled in ListMonk:
  - Go to **ListMonk Settings > Privacy > Individual subscriber tracking** → Turn **ON**
  - *Required for subscriber-level exports (views, clicks, bounces), opener false-positive filtering, and Auto-Unblock Protection.*

---

## Developed by

[WorthMind](https://worthmind.net/)
