"""Deliverability Service for ListMonk Pro.

Inspects Listmonk configuration against cold outreach standards, provides
a pre-flight deliverability audit score, and allows 1-click preset switching
between Newsletter Mode and Cold Email Mode.
"""

import logging
import re
from app.services.listmonk_client import ListMonkClient
from app.services.template_unsubscribe import get_unsubscribe_link_status

logger = logging.getLogger("deliverability_service")

# The audit is a taxonomy, not a flat list: the UI renders one block per group
# and prints the group's "blurb" as the section explainer. Keeping the labels
# here means adding a rule below only requires setting its `group` id.
CHECK_GROUPS = [
    {
        "id": "privacy",
        "label": "Privacy & tracking",
        "blurb": "What the message itself reveals about how it was sent.",
    },
    {
        "id": "sending",
        "label": "Sending engine",
        "blurb": "How fast ListMonk drains a campaign and how hard it hits each provider.",
    },
    {
        "id": "hygiene",
        "label": "List hygiene",
        "blurb": "Whether dead addresses leave the queue or get retried forever.",
    },
    {
        "id": "surface",
        "label": "Exposure & safety",
        "blurb": "What your instance publishes beyond the inbox.",
    },
]


def _duration_seconds(value) -> float:
    """Convert a ListMonk duration ('2m', '1h30m', 30) into seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().lower()
    if not text:
        return 0.0
    if text.isdigit():
        return float(text)
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    total = 0.0
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([smhd])", text):
        total += float(amount) * units[unit]
    return total


def _as_number(value, default: float) -> float:
    """Coerce a ListMonk setting to a number for threshold comparisons.

    ``raw_settings.get(key, default)`` only covers a *missing* key: an
    explicit ``null`` (or a numeric string from an older version) reached
    ``batch_size <= 100`` and raised TypeError, failing the whole audit
    with a 502.
    """
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def get_deliverability_checklist(listmonk: ListMonkClient) -> dict:
    """Fetch current Listmonk settings and run deliverability checks."""
    try:
        res = await listmonk.get_settings()
        raw_settings = res.get("data") or {}
    except Exception as e:
        logger.error(f"Failed to fetch Listmonk settings: {e}")
        return {
            "score": 0,
            "overall_status": "error",
            "error": str(e),
            "checks": [],
            "groups": CHECK_GROUPS,
            "raw_settings": {},
        }

    checks = []

    # 1. Tracking pixel disabled
    disable_tracking = raw_settings.get("privacy.disable_tracking")
    checks.append({
        "id": "disable_tracking",
        "group": "privacy",
        "name": "Tracking Pixel & Redirects Disabled",
        "description": "Zero 1x1 tracking pixels and no link rewrites prevent spam filter detection.",
        "status": "pass" if disable_tracking is True else "fail",
        "current_value": disable_tracking,
        "recommended": True,
        "impact": "Critical for cold email deliverability",
    })

    # 2. Individual tracking disabled
    ind_tracking = raw_settings.get("privacy.individual_tracking")
    checks.append({
        "id": "individual_tracking",
        "group": "privacy",
        "name": "Individual Tracking Disabled",
        "description": "Ensures no per-subscriber open tokens are appended to message bodies.",
        "status": "pass" if ind_tracking is False else "warn",
        "current_value": ind_tracking,
        "recommended": False,
        "impact": "High",
    })

    # 3. List-Unsubscribe Header. Required, not optional: since February 2024
    # Gmail and Yahoo demand RFC 8058 one-click unsubscribe (List-Unsubscribe
    # + List-Unsubscribe-Post) from senders of ~5,000+ messages a day, and
    # ListMonk only emits those headers with this setting on. Without them a
    # recipient's only exit is the spam button, and complaint rate is what
    # actually sinks a sending domain. (This check used to *require* the
    # header off, on the theory that it routes mail to Promotions.)
    unsub_header = raw_settings.get("privacy.unsubscribe_header")
    checks.append({
        "id": "unsubscribe_header",
        "group": "privacy",
        "name": "RFC List-Unsubscribe Header Enabled",
        "description": "Gmail and Yahoo require one-click List-Unsubscribe (RFC 8058) from bulk senders; "
                       "without it, recipients who want out can only press \"Report spam\", which damages "
                       "the sending domain's reputation far more than an unsubscribe.",
        "status": "pass" if unsub_header is True else "fail",
        "current_value": unsub_header,
        "recommended": True,
        "impact": "Critical for sender reputation",
    })

    # 3b. Footer unsubscribe link. The header above is invisible in the body;
    # the link people actually see comes from the campaign templates, so it is
    # read (and toggled) there. Unreadable templates degrade to "info" rather
    # than failing the whole audit.
    try:
        unsub_link = await get_unsubscribe_link_status(listmonk)
    except Exception as e:
        logger.warning(f"Could not read templates for the unsubscribe link check: {e}")
        unsub_link = {"state": "unknown", "counts": {}, "templates": [], "error": str(e)}
    link_state = unsub_link["state"]
    checks.append({
        "id": "unsubscribe_link",
        "group": "privacy",
        "name": "Unsubscribe Link in Email Footer",
        "description": "The visible opt-out link inside the message, rendered by your campaign templates. "
                       "CAN-SPAM, GDPR and CASL require a working opt-out in every commercial email; "
                       "hiding it pushes recipients to the spam button instead.",
        "status": {"on": "pass", "off": "warn", "mixed": "warn"}.get(link_state, "info"),
        "current_value": link_state,
        "recommended": True,
        "impact": "High",
    })

    # 4. Safe Batch Size
    batch_size = raw_settings.get("app.batch_size", 1000)
    checks.append({
        "id": "batch_size",
        "group": "sending",
        "name": "Sending Batch Size",
        "description": "Batches of <= 100 allow fine-grained pacing and prevent sudden provider rate limiting.",
        "status": "pass" if _as_number(batch_size, 1000) <= 100 else (
            "warn" if _as_number(batch_size, 1000) <= 250 else "fail"),
        "current_value": batch_size,
        "recommended": "<= 100",
        "impact": "High",
    })

    # 5. Concurrency
    concurrency = raw_settings.get("app.concurrency", 1)
    checks.append({
        "id": "concurrency",
        "group": "sending",
        "name": "Worker Concurrency",
        "description": "Concurrency of 1 worker maintains steady, human-like drip sending.",
        "status": "pass" if _as_number(concurrency, 0) == 1 else "warn",
        "current_value": concurrency,
        "recommended": 1,
        "impact": "Medium",
    })

    # 6. Public Archive / Subscription Page
    public_archive = raw_settings.get("app.enable_public_archive")
    checks.append({
        "id": "public_archive",
        "group": "surface",
        "name": "Public Archives Disabled",
        "description": "Public campaign pages expose outreach copy to scrapers and domain blacklists.",
        "status": "pass" if public_archive is False else "info",
        "current_value": public_archive,
        "recommended": False,
        "impact": "Low",
    })

    # 6b. Public subscription form — same exposure as the archive: a public
    # page under the sending domain that scrapers can enumerate.
    public_subscription = raw_settings.get("app.enable_public_subscription_page")
    checks.append({
        "id": "public_subscription_page",
        "group": "surface",
        "name": "Public Subscription Page Disabled",
        "description": "A public sign-up form lets bots subscribe arbitrary addresses (list bombing), "
                       "which then complain about mail they never asked for.",
        "status": "pass" if public_subscription is False else "info",
        "current_value": public_subscription,
        "recommended": False,
        "impact": "Low",
    })

    # 7. Bounce feedback loop. ListMonk can only apply hard-bounce rules
    # (bounce.actions) when it actually receives bounces; a placeholder mailbox
    # plus no provider/webhook means bounces are invisible to ListMonk and
    # invalid addresses keep getting mailed.
    bounce_enabled = raw_settings.get("bounce.enabled")
    mailboxes = raw_settings.get("bounce.mailboxes") or []
    active_mailboxes = sum(1 for m in mailboxes if isinstance(m, dict) and m.get("enabled"))
    provider_enabled = any([
        raw_settings.get("bounce.sendgrid_enabled"),
        raw_settings.get("bounce.ses_enabled"),
        raw_settings.get("bounce.webhooks_enabled"),
        (raw_settings.get("bounce.postmark") or {}).get("enabled"),
        (raw_settings.get("bounce.azure") or {}).get("enabled"),
        (raw_settings.get("bounce.forwardemail") or {}).get("enabled"),
        (raw_settings.get("bounce.lettermint") or {}).get("enabled"),
    ])
    bounce_actions = raw_settings.get("bounce.actions") or {}
    hard_action = (bounce_actions.get("hard") or {}).get("action")
    bounce_ok = bool(bounce_enabled and (active_mailboxes or provider_enabled))
    checks.append({
        "id": "bounce_feedback",
        "group": "hygiene",
        "name": "Bounce Feedback Loop Configured",
        "description": "ListMonk needs an enabled bounce mailbox or provider webhook before its hard-bounce "
                       "rules can blocklist dead addresses. Hard bounces that are never recorded stay in "
                       "the sending queue on every future campaign.",
        "status": "pass" if bounce_ok else "fail",
        "current_value": (
            f"enabled={bounce_enabled}, mailboxes={active_mailboxes}/{len(mailboxes)}, "
            f"providers={'yes' if provider_enabled else 'none'}"
        ),
        "recommended": "1 enabled mailbox or provider webhook",
        "impact": "Critical for list hygiene",
    })

    # 8. Hard-bounce action.
    checks.append({
        "id": "hard_bounce_action",
        "group": "hygiene",
        "name": "Hard Bounces Blocklist Automatically",
        "description": "Ensures a permanently invalid address is removed after the first hard bounce "
                       "instead of being retried on every campaign.",
        "status": "pass" if hard_action == "blocklist" else "warn",
        "current_value": hard_action or "unset",
        "recommended": "blocklist",
        "impact": "High",
    })

    # 9. Sending cadence. Not good/bad by itself — surfacing it prevents the
    # "campaign says finished but most of the list never got the mail" surprise.
    sliding = raw_settings.get("app.message_sliding_window")
    window_duration = raw_settings.get("app.message_sliding_window_duration")
    window_rate = raw_settings.get("app.message_sliding_window_rate")
    message_rate = raw_settings.get("app.message_rate")
    per_day = None
    try:
        seconds = _duration_seconds(window_duration)
        if sliding and seconds and window_rate:
            per_day = int(86400 / seconds * int(window_rate))
    except (TypeError, ValueError, ZeroDivisionError):
        per_day = None
    checks.append({
        "id": "send_cadence",
        "group": "sending",
        "name": "Sending Cadence",
        "description": "Window rate limits how fast a campaign drains. A very low ceiling keeps sends "
                       "human-like but can leave a large campaign queued for days.",
        "status": "info",
        "current_value": (
            f"window {window_rate}/{window_duration}, batch {raw_settings.get('app.batch_size')}, "
            f"concurrency {raw_settings.get('app.concurrency')}, "
            f"rate {message_rate}/s ⇒ ≈{per_day} msgs/day" if per_day else
            f"fixed rate {message_rate}/s"
        ),
        "recommended": "match the cadence to your list size",
        "impact": "Informational",
    })

    # 10. Media upload allow-list (the audit page is "Deliverability & Safety").
    extensions = raw_settings.get("upload.extensions") or []
    wildcard = "*" in extensions
    checks.append({
        "id": "upload_extensions",
        "group": "surface",
        "name": "Media Upload Types Restricted",
        "description": "A '*' wildcard in the allowed upload extensions lets an .html or .js file be "
                       "published under the upload URL, which the mail templates can then reference.",
        "status": "pass" if not wildcard else "fail",
        "current_value": ", ".join(extensions) if extensions else "unset",
        "recommended": "explicit image/document types only",
        "impact": "Medium",
    })

    # Calculate score. Informational checks are excluded so they cannot drag the
    # score down; they exist to surface context (e.g. a deliberate slow drip).
    scored = [c for c in checks if c["status"] != "info"]
    total_checks = len(scored)
    passed = sum(1 for c in scored if c["status"] == "pass")
    score = int((passed / total_checks) * 100) if total_checks else 0

    overall_status = "optimal" if score == 100 else ("good" if score >= 70 else "needs_attention")

    return {
        "score": score,
        "overall_status": overall_status,
        "checks": checks,
        "groups": CHECK_GROUPS,
        "raw_settings": {
            "disable_tracking": disable_tracking,
            "individual_tracking": ind_tracking,
            "unsubscribe_header": unsub_header,
            # Per-template footer link state (see template_unsubscribe).
            "unsubscribe_link": unsub_link,
            "public_archive": public_archive,
            "public_subscription_page": public_subscription,
            "batch_size": batch_size,
            "concurrency": concurrency,
            "bounce_enabled": bounce_enabled,
            "bounce_active_mailboxes": active_mailboxes,
            # Totals + the provider flag let the UI render the bounce loop as
            # discrete facts (mailbox count, webhook presence) rather than
            # re-parsing the "enabled=…, mailboxes=0/1" string above.
            "bounce_mailboxes_total": len(mailboxes),
            "bounce_provider_enabled": bool(provider_enabled),
            "hard_bounce_action": hard_action,
            "send_cadence_per_day": per_day,
            "message_rate": message_rate,
            "sliding_window": sliding,
            "sliding_window_rate": window_rate,
            "sliding_window_duration": window_duration,
            "upload_extensions": extensions,
        }
    }


async def apply_deliverability_preset(listmonk: ListMonkClient, mode: str) -> dict:
    """Apply optimized settings to Listmonk for the chosen mode."""
    if mode == "cold_email":
        payload = {
            "privacy.disable_tracking": True,
            "privacy.individual_tracking": False,
            # One-click unsubscribe stays on in cold mode too (see the audit
            # check above): it is a bulk-sender requirement, not a label.
            "privacy.unsubscribe_header": True,
            "app.batch_size": 100,
            "app.concurrency": 1,
            "app.max_send_errors": 10,
            "app.enable_public_archive": False,
            "app.enable_public_subscription_page": False,
        }
    elif mode == "newsletter":
        payload = {
            "privacy.disable_tracking": False,
            "privacy.individual_tracking": True,
            "privacy.unsubscribe_header": True,
            "app.batch_size": 1000,
            "app.concurrency": 2,
            "app.enable_public_archive": True,
            "app.enable_public_subscription_page": True,
        }
    else:
        raise ValueError(f"Unknown preset mode: {mode}")

    try:
        # ListMonk validates the whole settings payload, so a partial PUT is
        # rejected ("At least one SMTP block should be enabled"). apply_settings
        # merges the preset onto the current settings and blanks the masked
        # secrets so they are not overwritten with the mask.
        await listmonk.apply_settings(payload)
        logger.info(f"Applied {mode} preset settings to Listmonk")
        return {"status": "ok", "mode": mode, "applied": payload}
    except Exception as e:
        logger.error(f"Failed to apply {mode} preset: {e}")
        raise


# ── Individual setting edits ────────────────────────────────────────────────
# Every control on the Deliverability page maps onto one of these keys. The
# whitelist is the security boundary: the page can only change what the audit
# reports on, never SMTP credentials or any other block of ListMonk settings.
_BOOL = ("bool",)
EDITABLE_SETTINGS = {
    "privacy.disable_tracking": _BOOL,
    "privacy.individual_tracking": _BOOL,
    "privacy.unsubscribe_header": _BOOL,
    "app.enable_public_archive": _BOOL,
    "app.enable_public_subscription_page": _BOOL,
    "app.batch_size": ("int", 1, 10000),
    "app.concurrency": ("int", 1, 100),
    "app.message_rate": ("int", 1, 10000),
    "app.message_sliding_window": _BOOL,
    "app.message_sliding_window_rate": ("int", 1, 1000000),
    "app.message_sliding_window_duration": ("duration",),
    "bounce.enabled": _BOOL,
    # Virtual key: merged into the nested ``bounce.actions`` object so the
    # soft/complaint rules are preserved.
    "bounce.actions.hard": ("choice", {"none", "unsubscribe", "blocklist", "delete"}),
    "upload.extensions": ("extensions",),
}

# Go's time.ParseDuration subset ListMonk accepts in its settings UI.
_DURATION_RE = re.compile(r"^(?:\d+(?:h|m|s))+$")
_EXTENSION_RE = re.compile(r"^(?:\*|[a-z0-9]{1,10})$")


def validate_settings_patch(data) -> dict:
    """Validate a client patch against :data:`EDITABLE_SETTINGS`.

    Raises ValueError with a message naming the offending key (routers turn
    it into a 400). Never coerces: ``"true"`` is not a boolean and ``1.5`` is
    not a batch size, so a malformed body cannot write a surprising value.
    """
    if not isinstance(data, dict) or not data:
        raise ValueError("Body must be a non-empty object of setting → value")
    clean = {}
    for key, value in data.items():
        spec = EDITABLE_SETTINGS.get(key)
        if spec is None:
            raise ValueError(f"Setting '{key}' cannot be changed from this page")
        kind = spec[0]
        if kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false")
        elif kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} must be a whole number")
            low, high = spec[1], spec[2]
            if not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
        elif kind == "duration":
            if not isinstance(value, str) or not _DURATION_RE.match(value.strip().lower()):
                raise ValueError(f"{key} must be a duration like 1h, 30m or 1h30m")
            value = value.strip().lower()
            if _duration_seconds(value) <= 0:
                raise ValueError(f"{key} must be longer than zero")
        elif kind == "choice":
            if value not in spec[1]:
                raise ValueError(f"{key} must be one of: {', '.join(sorted(spec[1]))}")
        elif kind == "extensions":
            if not isinstance(value, list) or not value or len(value) > 50:
                raise ValueError(f"{key} must be a list of 1–50 file extensions")
            exts = []
            for ext in value:
                if not isinstance(ext, str):
                    raise ValueError(f"{key} entries must be text")
                ext = ext.strip().lower().lstrip(".")
                if not _EXTENSION_RE.match(ext):
                    raise ValueError(f"Invalid file extension: {ext!r}")
                if ext not in exts:
                    exts.append(ext)
            value = exts
        clean[key] = value
    return clean


async def update_deliverability_settings(listmonk: ListMonkClient, patch: dict) -> dict:
    """Apply a validated patch (see :func:`validate_settings_patch`)."""
    payload = dict(patch)
    hard_action = payload.pop("bounce.actions.hard", None)
    if hard_action is not None:
        current = (await listmonk.get_settings()).get("data") or {}
        actions = dict(current.get("bounce.actions") or {})
        hard = dict(actions.get("hard") or {})
        hard.setdefault("count", 1)
        hard["action"] = hard_action
        actions["hard"] = hard
        payload["bounce.actions"] = actions
    # apply_settings merges onto the full current payload (rule 12).
    await listmonk.apply_settings(payload)
    logger.info(f"Updated deliverability settings: {sorted(patch)}")
    return {"status": "ok", "applied": patch}
