"""
Simple session-based authentication for the dashboard.
Uses a username/password from .env and signed cookies.

A session token is ``<issued-at>.<nonce>:<signature>``. The signature key is
derived from SESSION_SECRET *and* the dashboard credentials, so changing
DASHBOARD_USER/DASHBOARD_PASS (and restarting) invalidates every session ever
issued. Logging out revokes the token's nonce in ``session_revocations.json``
(DATA_DIR), so a copied cookie stops working the moment its owner logs out
instead of staying valid for the rest of its 7 days.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from fastapi import Request, Response
from app.config import settings

logger = logging.getLogger("auth")

# Session cookie name
COOKIE_NAME = "lmpro_session"
# Session duration: 7 days
SESSION_MAX_AGE = 7 * 24 * 60 * 60

# Secret key for signing cookies (auto-generated on first run, persisted to DATA_DIR or .env)

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Login rate limiting (in-memory, per client IP)
LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW_SECONDS = 15 * 60
_MAX_TRACKED_IPS = 10_000
_login_failures: dict[str, list[float]] = {}


def is_login_rate_limited(ip: str) -> bool:
    """True if this IP has too many recent failed login attempts."""
    now = time.time()
    attempts = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
    if attempts:
        _login_failures[ip] = attempts
    return len(attempts) >= LOGIN_MAX_ATTEMPTS


def record_login_failure(ip: str) -> None:
    """Record a failed login attempt for an IP (bounded memory)."""
    if len(_login_failures) >= _MAX_TRACKED_IPS:
        now = time.time()
        for tracked_ip in list(_login_failures):
            attempts = [t for t in _login_failures[tracked_ip] if now - t < LOGIN_WINDOW_SECONDS]
            if attempts:
                _login_failures[tracked_ip] = attempts
            else:
                _login_failures.pop(tracked_ip, None)
        # Still full: evict the OLDEST buckets instead of clearing every one.
        # clear() reset the counts of IPs that were actively locked out, so a
        # flood of spoofed X-Forwarded-For addresses could reach the cap and
        # periodically unlock every concurrent brute-force source.
        while len(_login_failures) >= _MAX_TRACKED_IPS:
            _login_failures.pop(next(iter(_login_failures)), None)
    _login_failures.setdefault(ip, []).append(time.time())


def clear_login_failures(ip: str) -> None:
    _login_failures.pop(ip, None)


def client_ip_from_request(request: Request) -> str:
    """Resolve the real client IP for rate limiting.

    Behind a reverse proxy ``request.client.host`` is the *proxy*, so every
    visitor shares one bucket and a handful of failed logins locks everyone
    out. ``X-Forwarded-For`` carries the original client, but it is
    client-controlled: only honour it when the immediate peer is a trusted
    proxy, otherwise an attacker can spoof a fresh IP per attempt and defeat
    the limit entirely.
    """
    peer = request.client.host if request.client else "unknown"

    trusted = os.getenv("TRUST_PROXY", "").strip().lower()
    if trusted in ("1", "true", "yes"):
        pass  # deployment states it always sits behind a trusted proxy
    elif trusted in ("0", "false", "no"):
        return peer
    else:
        # Default: only trust loopback/private peers (typical same-host Nginx).
        try:
            import ipaddress
            addr = ipaddress.ip_address(peer)
            trusted_peer = addr.is_loopback or addr.is_private
        except ValueError:
            trusted_peer = False
        if not trusted_peer:
            return peer

    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        # Right-most entry is the client IP as seen by the immediate trusted
        # proxy: nginx's $proxy_add_x_forwarded_for APPENDS the peer, so the
        # last entry is the one the client cannot spoof. Reading the left-most
        # entry instead would let an attacker rotate a fresh rate-limit bucket
        # per attempt with a fake header.
        # NOTE: This assumes a single trusted proxy between the client and
        # the app. In multi-hop chains where intermediate proxies also append,
        # the rightmost entry may be an intermediate proxy rather than the
        # real client. For those setups, configure TRUST_PROXY=false and
        # terminate TLS at the outermost proxy.
        entries = [e.strip() for e in forwarded.split(",") if e.strip()]
        if entries:
            return entries[-1]
    return peer


def _load_or_create_secret_key() -> str:
    """Load SESSION_SECRET from env, persisted key file in DATA_DIR, or generate a new one."""
    key = os.getenv("SESSION_SECRET", "")
    if key:
        return key

    key_file = settings.data_path("session_secret.key")
    if key_file.exists():
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
        try:
            persisted = key_file.read_text(encoding="utf-8").strip()
            if persisted:
                os.environ["SESSION_SECRET"] = persisted
                return persisted
        except OSError:
            pass

    key = secrets.token_hex(32)

    # Persist to DATA_DIR so sessions survive restarts across Docker & dev
    try:
        key_file.write_text(key, encoding="utf-8")
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
        os.environ["SESSION_SECRET"] = key
        logger.info("Generated and persisted SESSION_SECRET to %s", key_file)
        return key
    except OSError:
        pass

    # Fallback to appending to .env if DATA_DIR write fails.
    # Check for an existing SESSION_SECRET line first to avoid duplicates
    # (python-dotenv uses the first occurrence, so later appends are ignored).
    dotenv_path = _REPO_ROOT / ".env"
    try:
        existing = dotenv_path.read_text(encoding="utf-8") if dotenv_path.exists() else ""
        if "SESSION_SECRET=" not in existing:
            with open(dotenv_path, "a", encoding="utf-8") as f:
                f.write(f"\nSESSION_SECRET={key}\n")
        os.environ["SESSION_SECRET"] = key
        logger.info("Generated and persisted SESSION_SECRET to .env")
    except OSError:
        logger.warning(
            "SESSION_SECRET not set and could not be persisted. "
            "Sessions will break on restart. "
            "Set SESSION_SECRET in .env for persistent sessions."
        )
    return key


_secret_key = _load_or_create_secret_key()


def _signing_key() -> bytes:
    """Session signing key bound to the secret AND the dashboard credentials.

    Signing with SESSION_SECRET alone meant a password change left every
    previously issued cookie valid for up to 7 more days — rotating a leaked
    password did not lock out whoever held a session. Folding the credentials
    into the key makes a credential change (and restart) a global logout.
    """
    creds = "\0".join((os.getenv("DASHBOARD_USER", ""), os.getenv("DASHBOARD_PASS", "")))
    return hmac.new(
        _secret_key.encode(), b"lmpro-session-v2\0" + creds.encode("utf-8"), hashlib.sha256,
    ).digest()


def _sign(value: str) -> str:
    """Create HMAC signature for a value."""
    return hmac.new(_signing_key(), value.encode(), hashlib.sha256).hexdigest()


# ── Logout revocation ─────────────────────────────────────────────────────
#
# Tokens are stateless, so logout used to only delete the browser's copy of
# the cookie. The nonces of logged-out sessions are persisted (with the
# token's own expiry, after which the signature check rejects it anyway), and
# the file is re-read whenever it changes on disk so several workers agree.
REVOCATIONS_FILE = settings.data_path("session_revocations.json")
_revoked: dict[str, float] = {}
_revoked_mtime: Optional[float] = None
_revoked_lock = threading.RLock()  # re-entrant: revoke_session reads under it


def _current_revocations() -> dict[str, float]:
    """Revoked nonce -> expiry, refreshed from disk when the file changed."""
    global _revoked, _revoked_mtime
    try:
        mtime = REVOCATIONS_FILE.stat().st_mtime
    except OSError:
        return _revoked  # nothing persisted (or unreadable): memory is authoritative
    if mtime != _revoked_mtime:
        with _revoked_lock:
            try:
                data = json.loads(REVOCATIONS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _revoked = {
                        str(k): float(v) for k, v in data.items()
                        if isinstance(v, (int, float)) and not isinstance(v, bool)
                    }
            except (OSError, ValueError):
                pass
            _revoked_mtime = mtime
    return _revoked


def _parse_token(token: Optional[str]) -> Optional[tuple[int, str]]:
    """Return ``(issued_at, nonce)`` for a well-formed, correctly signed token."""
    if not token or ":" not in token:
        return None
    payload, signature = token.rsplit(":", 1)
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    issued, sep, nonce = payload.partition(".")
    if not sep or not nonce:
        return None  # pre-revocation token format: cannot be revoked, so not accepted
    try:
        return int(issued), nonce
    except ValueError:
        return None


def revoke_session(token: Optional[str]) -> bool:
    """Revoke one session token server-side (blocking file write; run off-loop).

    Returns True when a valid token was revoked. Expired revocations are
    pruned on every write, so the file only ever holds live sessions.
    """
    parsed = _parse_token(token)
    if parsed is None:
        return False
    issued, nonce = parsed
    global _revoked, _revoked_mtime
    now = time.time()
    with _revoked_lock:
        current = {k: v for k, v in _current_revocations().items() if v > now}
        current[nonce] = float(issued + SESSION_MAX_AGE)
        _revoked = current
        tmp_path = None
        try:
            REVOCATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", dir=REVOCATIONS_FILE.parent, prefix=REVOCATIONS_FILE.name,
                suffix=".tmp", delete=False, encoding="utf-8",
            ) as f:
                tmp_path = f.name
                json.dump(current, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, REVOCATIONS_FILE)
            _revoked_mtime = REVOCATIONS_FILE.stat().st_mtime
        except OSError as e:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            # The in-memory revocation still applies to this process.
            logger.warning(f"Could not persist session revocation: {e}")
    return True


def _is_secure_cookie(request: Optional[Request] = None) -> bool:
    cookie_secure_env = os.getenv("COOKIE_SECURE", "").strip().lower()
    if cookie_secure_env in ("1", "true", "yes"):
        return True
    if cookie_secure_env in ("0", "false", "no"):
        return False
    if request is not None:
        if request.url.scheme == "https":
            return True
        if request.headers.get("x-forwarded-proto", "").lower() == "https":
            return True
        return False
    return False


def create_session(response: Response, request: Optional[Request] = None):
    """Set a signed session cookie on the response."""
    payload = f"{int(time.time())}.{secrets.token_hex(16)}"
    token = f"{payload}:{_sign(payload)}"
    is_secure = _is_secure_cookie(request)
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=is_secure,
        path="/",
    )


def verify_session(request: Request) -> bool:
    """Check if the request has a valid session cookie."""
    parsed = _parse_token(request.cookies.get(COOKIE_NAME))
    if parsed is None:
        return False
    created, nonce = parsed

    # Check expiry
    if time.time() - created > SESSION_MAX_AGE:
        return False

    # Logged out on this or another worker
    return nonce not in _current_revocations()


def clear_session(response: Response, request: Optional[Request] = None):
    """Remove the session cookie."""
    is_secure = _is_secure_cookie(request)
    response.delete_cookie(
        COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=is_secure,
        path="/",
    )


def _constant_time_equals(a: str, b: str) -> bool:
    """Compare two strings in constant time.

    ``hmac.compare_digest`` raises ``TypeError`` on ``str`` inputs containing
    non-ASCII characters, which would turn a bad password into a 500. Encoding
    to UTF-8 first keeps the comparison constant-time for any input and makes
    non-ASCII credentials a normal authentication failure.
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


_KNOWN_DEFAULT_CREDS = {
    ("admin", "changeme"),
    ("admin", "admin"),
    ("admin", "password"),
}


def warn_if_default_credentials() -> None:
    """Log a warning at startup when credentials match known defaults."""
    user = os.getenv("DASHBOARD_USER", "")
    passwd = os.getenv("DASHBOARD_PASS", "")
    if (user, passwd) in _KNOWN_DEFAULT_CREDS:
        logger.warning(
            "⚠️  DASHBOARD_USER/DASHBOARD_PASS match known default credentials "
            "(%s/****). Change them immediately to prevent unauthorized access.",
            user,
        )


def check_credentials(username: str, password: str) -> bool:
    """Validate login credentials against DASHBOARD_USER / DASHBOARD_PASS env vars.

    Returns False when either env var is unset, so a misconfigured deployment
    is locked rather than silently accepting default credentials.
    """
    valid_user = os.getenv("DASHBOARD_USER", "")
    valid_pass = os.getenv("DASHBOARD_PASS", "")
    if not valid_user or not valid_pass:
        logger.error(
            "DASHBOARD_USER and DASHBOARD_PASS must be set in .env. "
            "Login is disabled until credentials are configured."
        )
        return False
    # Both sides are always evaluated so a wrong username does not return
    # measurably faster than a wrong password.
    user_ok = _constant_time_equals(username, valid_user)
    pass_ok = _constant_time_equals(password, valid_pass)
    return user_ok and pass_ok
