import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
import httpx

from app.services.listmonk_client import listmonk
from app.services.auto_unblock import (
    find_blocklisted_engaged,
    unblock_subscribers,
    QUERY_BLOCKLISTED_ENGAGED,
)
from app.services.campaign_scheduler import (
    load_schedule, is_within_send_window,
    scheduler_loop, run_scheduler_tick, update_schedule_settings,
)
from app.services.imap_unsubscribe import scan_and_unsubscribe
from app.services.link_unsubscribe import scan_link_unsubscribes
from app.services.bounce_ingest import ingest_bounce_mailbox
from app.services.hard_bounce_cache import start_cache_updater, schedule_hard_bounce_update
from app.auth import (
    verify_session, create_session, clear_session, check_credentials,
    is_login_rate_limited, record_login_failure, clear_login_failures,
    client_ip_from_request, warn_if_default_credentials,
)
from app.services.task_utils import shutdown as shutdown_tasks
from app.services.imap_reply_tracker import scan_and_track_replies
from app.routers import (
    subscribers, lists, campaigns, templates, bounces, converter, unsubscribes,
    replies, deliverability, appsettings,
)

logger = logging.getLogger("listmonk-dashboard")
BASE_DIR = Path(__file__).resolve().parent.parent

# The app logs through named loggers all over (scan results, prune counts,
# scheduler ticks, bounce classification) but uvicorn only configures its own
# loggers — without this every logger.info() is discarded and the diagnostics
# are invisible. Idempotent so a --reload reload does not stack handlers.
def _configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
        ))
        root.addHandler(handler)
    root.setLevel(level)


# Configured at import time on purpose: uvicorn imports the app before it runs
# the lifespan, and any module imported from here (auth's secret bootstrap, the
# service layer) logs during import. Configuring later would silently discard
# those first messages. Idempotent so a --reload reload does not stack handlers.
_configure_logging()

AUTO_UNBLOCK_INTERVAL = 6 * 60 * 60
IMAP_SCAN_INTERVAL = 60 * 60  # 1 hour
BOUNCE_INGEST_INTERVAL = 60 * 60  # 1 hour
REPLY_SCAN_INTERVAL = 15 * 60  # 15 minutes
_auto_unblock_task = None
_scheduler_task = None
_imap_scan_task = None
_bounce_ingest_task = None
_hard_bounce_cache_task = None
_reply_scan_task = None


# ── Auth Middleware ───────────────────────────────────────

def _is_https(request: Request) -> bool:
    """True when the client reached us over TLS, directly or via a proxy."""
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").lower() == "https"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach baseline security headers to every response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        # CSP provides defense-in-depth against XSS. 'unsafe-inline' is
        # required for the inline theme-init script and onclick handlers;
        # a future migration to nonce-based CSP can tighten this.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self' https://fonts.gstatic.com; "
            "connect-src 'self'; frame-src 'none'; object-src 'none'",
        )
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        # HSTS is only meaningful (and only safe) over TLS — sending it on a
        # plain-HTTP dev instance would make browsers refuse to ever retry.
        if _is_https(request):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class CSRFMiddleware(BaseHTTPMiddleware):
    """Reject state-changing API requests that lack a CORS-preflight trigger.

    Browsers will not send ``X-Requested-With`` or ``Content-Type:
    application/json`` cross-origin without a preflight, so requiring one of
    these headers on POST/PUT/DELETE blocks cross-site form submissions even
    when SameSite cookies are bypassed (e.g. same-origin XSS). GET requests
    and non-API paths are unaffected.
    """

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    async def dispatch(self, request: Request, call_next):
        if request.method not in self.SAFE_METHODS and request.url.path.startswith("/api/"):
            has_xrw = request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"
            has_json_ct = "application/json" in (request.headers.get("content-type") or "")
            is_form_upload = "multipart/form-data" in (request.headers.get("content-type") or "")
            if not (has_xrw or has_json_ct or is_form_upload):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Missing X-Requested-With or Content-Type header"},
                )
        return await call_next(request)


class AuthMiddleware(BaseHTTPMiddleware):
    """Protect all routes except login and static files."""

    OPEN_PATHS = {"/auth/login", "/auth/logout", "/favicon.ico", "/health"}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Allow static files, login page, and auth endpoints
        if path.startswith("/static") or path in self.OPEN_PATHS:
            return await call_next(request)

        # Check session
        if not verify_session(request):
            # API calls get 401, browser gets redirect
            if path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
            return RedirectResponse("/auth/login", status_code=302)

        return await call_next(request)


# ── Background Tasks ─────────────────────────────────────

_BACKOFF_MAX = 3600  # Cap exponential backoff at 1 hour


def _backoff_delay(consecutive_failures: int, base_interval: int) -> int:
    """Exponential backoff: double the delay on each consecutive failure,
    capped at ``_BACKOFF_MAX``. A successful run resets the counter."""
    if consecutive_failures <= 0:
        return base_interval
    return min(base_interval * (2 ** consecutive_failures), _BACKOFF_MAX)


async def auto_unblock_loop():
    failures = 0
    while True:
        try:
            subs = await find_blocklisted_engaged(listmonk)
            if subs:
                result = await unblock_subscribers(listmonk, subs)
                logger.info(f"Auto-unblock: {result['success']} unblocked, {result['failed']} failed")
            failures = 0
        except Exception as e:
            failures += 1
            logger.error(f"Auto-unblock error (#{failures}): {e}")
        await asyncio.sleep(_backoff_delay(failures, AUTO_UNBLOCK_INTERVAL))


async def imap_scan_loop():
    """Scan IMAP inbox and ListMonk link unsubscribes every hour, starting immediately on startup."""
    failures = 0
    while True:
        try:
            imap_result = await scan_and_unsubscribe(listmonk)
            if imap_result.get("processed", 0) > 0:
                logger.info(f"IMAP scan: {imap_result['processed']} unsubscribe(s) processed")
            failures = 0
        except Exception as e:
            failures += 1
            logger.error(f"IMAP scan error (#{failures}): {e}")
        try:
            link_result = await scan_link_unsubscribes(listmonk)
            if link_result.get("processed", 0) > 0:
                logger.info(f"Link scan: {link_result['processed']} unsubscribe(s) processed")
        except Exception as e:
            logger.error(f"Link unsubscribe scan error: {e}")
        await asyncio.sleep(_backoff_delay(failures, IMAP_SCAN_INTERVAL))


async def bounce_ingest_loop():
    """Ingest new bounces from the IMAP mailbox into ListMonk every hour."""
    failures = 0
    while True:
        try:
            result = await ingest_bounce_mailbox(listmonk)
            if result.get("ingested", 0) > 0:
                logger.info(
                    f"Bounce ingest: {result['ingested']} ingested "
                    f"(hard={result.get('hard', 0)}, soft={result.get('soft', 0)})"
                )
                schedule_hard_bounce_update()
            failures = 0
        except Exception as e:
            failures += 1
            logger.error(f"Bounce ingest error (#{failures}): {e}")
        await asyncio.sleep(_backoff_delay(failures, BOUNCE_INGEST_INTERVAL))


async def reply_scan_loop():
    """Scan IMAP for cold outreach replies every 15 minutes."""
    failures = 0
    while True:
        try:
            result = await scan_and_track_replies(listmonk)
            if result.get("new_replies", 0) > 0:
                logger.info(f"Reply scan: {result['new_replies']} new prospect reply(ies) recorded")
            failures = 0
        except Exception as e:
            failures += 1
            logger.error(f"Reply scan error (#{failures}): {e}")
        await asyncio.sleep(_backoff_delay(failures, REPLY_SCAN_INTERVAL))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _auto_unblock_task, _scheduler_task, _imap_scan_task, _bounce_ingest_task, _hard_bounce_cache_task, _reply_scan_task
    await listmonk.start()
    _auto_unblock_task = asyncio.create_task(auto_unblock_loop())
    _scheduler_task = asyncio.create_task(scheduler_loop(listmonk))
    _imap_scan_task = asyncio.create_task(imap_scan_loop())
    _bounce_ingest_task = asyncio.create_task(bounce_ingest_loop())
    _hard_bounce_cache_task = asyncio.create_task(start_cache_updater())
    _reply_scan_task = asyncio.create_task(reply_scan_loop())
    logger.info("Background tasks started: auto-unblock (6h), campaign scheduler (60s), IMAP+link scan (1h), bounce ingest (1h), hard bounce cache (5min), reply tracker (15min)")
    warn_if_default_credentials()
    logger.warning(
        "Login rate-limit state is in-memory only and was reset on this restart. "
        "An attacker who triggers container restarts can bypass the 10-attempt/15-minute limit."
    )
    # Warn when running behind a proxy without confirmed TLS — the session
    # cookie's Secure flag depends on X-Forwarded-Proto being set correctly.
    if os.getenv("TRUST_PROXY", "").lower() == "true":
        logger.warning(
            "TRUST_PROXY=true is set. Ensure your reverse proxy sends "
            "X-Forwarded-Proto: https, otherwise session cookies will be "
            "sent without the Secure flag over plain HTTP."
        )
    yield
    loop_tasks = [
        _auto_unblock_task, _scheduler_task, _imap_scan_task,
        _bounce_ingest_task, _hard_bounce_cache_task, _reply_scan_task,
    ]
    for task in loop_tasks:
        task.cancel()
    await asyncio.gather(*loop_tasks, return_exceptions=True)
    # Also drain helper tasks spawned via task_utils.spawn().
    await shutdown_tasks()
    await listmonk.close()


# ── App Setup ────────────────────────────────────────────

app = FastAPI(title="ListMonk Pro", lifespan=lifespan)
# Middleware execution order (outermost first): SecurityHeaders → Auth → CSRF → routes.
# CSRF runs after auth so only authenticated requests are checked.
app.add_middleware(CSRFMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)


@app.exception_handler(httpx.HTTPStatusError)
async def httpx_error_handler(request: Request, exc: httpx.HTTPStatusError):
    # ListMonk API errors are forwarded to the SPA. Upstream *auth* failures
    # are reported as 502: the frontend treats every 401 as its own session
    # expiry and redirects to /auth/login, so echoing ListMonk's 401 for a bad
    # LISTMONK_USER/API key would trap the user in a login loop.
    upstream_status = exc.response.status_code
    status = 502 if upstream_status in (401, 403) else upstream_status

    detail = str(exc)
    try:
        payload = exc.response.json()
        if isinstance(payload, dict):
            detail = payload.get("message") or payload.get("error") or detail
    except Exception:
        pass
    if status == 502:
        detail = (
            f"ListMonk rejected the dashboard's API credentials "
            f"(HTTP {upstream_status}). Check LISTMONK_USER and "
            f"LISTMONK_API_KEY. Upstream response: {detail}"
        )
    return JSONResponse(status_code=status, content={"detail": detail})


@app.exception_handler(httpx.TransportError)
async def httpx_transport_error_handler(request: Request, exc: httpx.TransportError):
    """Report an unreachable ListMonk as 502 instead of an opaque 500."""
    logger.error(f"ListMonk connection failed: {exc}")
    return JSONResponse(
        status_code=502,
        content={"detail": f"Cannot reach the ListMonk API: {exc}"},
    )

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
# autoescape is ON: no template interpolates user data today, but leaving it
# off makes any future {{ value }} an XSS by default.
jinja_env = Environment(
    loader=FileSystemLoader(BASE_DIR / "templates"),
    autoescape=True,
)

# Include routers
app.include_router(subscribers.router, prefix="/api/subscribers", tags=["Subscribers"])
app.include_router(lists.router, prefix="/api/lists", tags=["Lists"])
app.include_router(campaigns.router, prefix="/api/campaigns", tags=["Campaigns"])
app.include_router(templates.router, prefix="/api/templates", tags=["Templates"])
app.include_router(bounces.router, prefix="/api/bounces", tags=["Bounces"])
app.include_router(converter.router, prefix="/api/converter", tags=["CSV Converter"])
app.include_router(unsubscribes.router, prefix="/api/unsubscribes", tags=["Unsubscribes"])
app.include_router(replies.router, prefix="/api/replies", tags=["Replies"])
app.include_router(deliverability.router, prefix="/api/deliverability", tags=["Deliverability"])
app.include_router(appsettings.router, prefix="/api/app-settings", tags=["App Settings"])


# ── Template Helper ───────────────────────────────────────

def render_template(name: str, context: dict) -> HTMLResponse:
    template = jinja_env.get_template(name)
    return HTMLResponse(template.render(**context))


# ── Auth Routes ──────────────────────────────────────────

@app.get("/auth/login")
async def login_page(request: Request):
    if verify_session(request):
        return RedirectResponse("/", status_code=302)
    return render_template("login.html", {"request": request})


@app.post("/auth/login")
async def login(request: Request):
    client_ip = client_ip_from_request(request)
    if is_login_rate_limited(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts. Please try again later.",
        )

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Invalid request body")

    username = data.get("username", "")
    password = data.get("password", "")

    # Non-string JSON values (numbers, arrays, objects) used to reach the
    # constant-time compare and raise AttributeError → 500, skipping the
    # rate limiter entirely. Reject them as a normal client error and still
    # count the attempt.
    if not isinstance(username, str) or not isinstance(password, str):
        record_login_failure(client_ip)
        raise HTTPException(status_code=400, detail="username and password must be strings")

    if not check_credentials(username, password):
        record_login_failure(client_ip)
        raise HTTPException(status_code=401, detail="Invalid username or password")

    clear_login_failures(client_ip)
    response = JSONResponse({"status": "ok"})
    create_session(response, request)
    return response


# Logout is state-changing, so it is POST-only. A GET would be triggerable by
# any cross-site <img>/<link> — SameSite=Lax still sends the cookie on
# top-level navigations, so a GET logout is a trivial forced-logout vector.
@app.post("/auth/logout")
async def logout(request: Request):
    response = RedirectResponse("/auth/login", status_code=303)
    clear_session(response, request)
    return response


# ── Favicon & Dashboard ───────────────────────────────────

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(BASE_DIR / "static" / "favicon.png", media_type="image/png")


@app.get("/health", include_in_schema=False)
async def health():
    """Liveness probe for the container healthcheck.

    Deliberately does no template rendering and makes no upstream call: a
    ListMonk outage must not mark this container unhealthy (the app serves
    its own error UI), and the probe must stay cheap enough to run every
    30 seconds forever.
    """
    return JSONResponse({"status": "ok"})


@app.get("/")
async def index(request: Request):
    return render_template("index.html", {"request": request})


# ── Auto-Unblock Endpoints ───────────────────────────────

@app.get("/api/auto-unblock/status")
async def auto_unblock_status():
    # Deliberately not a 5xx: the Settings page loads this alongside the
    # scheduler with Promise.all, so raising here would blank the whole page
    # when only this readout is unavailable. The client renders `error`.
    try:
        result = await listmonk.get_subscribers(1, 1, QUERY_BLOCKLISTED_ENGAGED)
        total = (result.get("data") or {}).get("total") or 0
        return {
            "blocklisted_engaged": total,
            "blocklisted_clickers": total,
            "interval_hours": AUTO_UNBLOCK_INTERVAL // 3600,
        }
    except Exception as e:
        logger.error(f"auto-unblock status failed: {e}")
        return {
            "error": str(e),
            "blocklisted_engaged": 0,
            "blocklisted_clickers": 0,
            "interval_hours": AUTO_UNBLOCK_INTERVAL // 3600,
        }


@app.post("/api/auto-unblock/run")
async def auto_unblock_run_now():
    try:
        subs = await find_blocklisted_engaged(listmonk)
        if not subs:
            return {"success": 0, "failed": 0, "unblocked": [], "message": "No blocklisted engaged subscribers found"}
        return await unblock_subscribers(listmonk, subs)
    except Exception as e:
        logger.error(f"auto-unblock run failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Auto-unblock failed: {e}")


# ── Campaign Scheduler Endpoints ─────────────────────────

@app.get("/api/scheduler")
async def get_schedule():
    schedule = load_schedule()
    tz = ZoneInfo(schedule["timezone"])
    now = datetime.now(tz)
    in_window = is_within_send_window(schedule) if schedule["enabled"] else None
    return {
        **schedule,
        "current_time": now.strftime("%A %I:%M %p %Z"),
        "in_send_window": in_window,
    }


@app.put("/api/scheduler")
async def update_schedule(data: dict):
    day_abbr_map = {
        "mon": "mon", "monday": "mon",
        "tue": "tue", "tuesday": "tue",
        "wed": "wed", "wednesday": "wed",
        "thu": "thu", "thursday": "thu",
        "fri": "fri", "friday": "fri",
        "sat": "sat", "saturday": "sat",
        "sun": "sun", "sunday": "sun",
    }
    if "timezone" in data:
        try:
            ZoneInfo(str(data["timezone"]))
        except Exception:
            raise HTTPException(status_code=400, detail=f"Invalid timezone: {data['timezone']}")

    for hour_key in ["start_hour", "end_hour"]:
        if hour_key in data:
            val = data[hour_key]
            if isinstance(val, bool) or not isinstance(val, int) or val < 0 or val > 23:
                raise HTTPException(status_code=400, detail=f"{hour_key} must be an integer between 0 and 23")

    for min_key in ["start_minute", "end_minute"]:
        if min_key in data:
            val = data[min_key]
            if isinstance(val, bool) or not isinstance(val, int) or val < 0 or val > 59:
                raise HTTPException(status_code=400, detail=f"{min_key} must be an integer between 0 and 59")

    if "days" in data:
        if not isinstance(data["days"], list):
            raise HTTPException(status_code=400, detail="days must be a list of weekday names")
        normalized_days = []
        for day in data["days"]:
            day_str = str(day).lower().strip()
            if day_str not in day_abbr_map:
                raise HTTPException(status_code=400, detail=f"Invalid day in days list: {day}")
            normalized_days.append(day_abbr_map[day_str])
        # An empty day set means no time is ever inside the window, so every
        # running campaign would be auto-paused and never resumed. Reject it
        # instead of silently stopping all sending.
        if not normalized_days:
            raise HTTPException(
                status_code=400,
                detail="days must contain at least one weekday",
            )
        data["days"] = normalized_days

    if "enabled" in data and not isinstance(data["enabled"], bool):
        raise HTTPException(status_code=400, detail="enabled must be a boolean")

    # Persist through the scheduler's locked service API: a raw
    # load → merge → save here could be clobbered by (or clobber) a tick
    # running concurrently with this request.
    try:
        schedule = await update_schedule_settings(data)
    except OSError as e:
        logger.error(f"Could not persist schedule: {e}")
        raise HTTPException(
            status_code=500,
            detail="Could not write schedule file (check data directory permissions).",
        )
    if schedule.get("enabled"):
        await run_scheduler_tick(listmonk)
    return {"status": "ok", "schedule": schedule}


@app.post("/api/scheduler/run")
async def scheduler_run_now():
    try:
        await run_scheduler_tick(listmonk)
    except Exception as e:
        logger.error(f"scheduler run failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Scheduler run failed: {e}")
    schedule = load_schedule()
    return {
        "status": "ok",
        "in_send_window": is_within_send_window(schedule),
        "auto_paused_campaigns": schedule.get("auto_paused_campaigns", []),
    }


