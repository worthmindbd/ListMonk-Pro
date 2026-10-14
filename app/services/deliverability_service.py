"""Deliverability Service for ListMonk Pro.

Inspects Listmonk configuration against cold outreach standards, provides
a pre-flight deliverability audit score, and allows 1-click preset switching
between Newsletter Mode and Cold Email Mode.
"""

import logging
import re
from app.services.listmonk_client import ListMonkClient

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

    # 3. List-Unsubscribe Header
    unsub_header = raw_settings.get("privacy.unsubscribe_header")
    checks.append({
        "id": "unsubscribe_header",
        "group": "privacy",
        "name": "RFC List-Unsubscribe Header Disabled",
        "description": "Email clients (Gmail/Outlook) automatically route messages with this header to Promotions or Spam.",
        "status": "pass" if unsub_header is False else "fail",
        "current_value": unsub_header,
        "recommended": False,
        "impact": "Critical for primary inboxing",
    })

    # 4. Safe Batch Size
    batch_size = raw_settings.get("app.batch_size", 1000)
    checks.append({
        "id": "batch_size",
        "group": "sending",
        "name": "Sending Batch Size",
        "description": "Batches of <= 100 allow fine-grained pacing and prevent sudden provider rate limiting.",
        "status": "pass" if batch_size <= 100 else ("warn" if batch_size <= 250 else "fail"),
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
        "status": "pass" if concurrency == 1 else "warn",
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
            "upload_extensions": extensions,
        }
    }


async def apply_deliverability_preset(listmonk: ListMonkClient, mode: str) -> dict:
    """Apply optimized settings to Listmonk for the chosen mode."""
    if mode == "cold_email":
        payload = {
            "privacy.disable_tracking": True,
            "privacy.individual_tracking": False,
            "privacy.unsubscribe_header": False,
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
