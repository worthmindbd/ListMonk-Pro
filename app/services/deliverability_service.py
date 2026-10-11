"""Deliverability Service for ListMonk Pro.

Inspects Listmonk configuration against cold outreach standards, provides
a pre-flight deliverability audit score, and allows 1-click preset switching
between Newsletter Mode and Cold Email Mode.
"""

import logging
from app.services.listmonk_client import ListMonkClient

logger = logging.getLogger("deliverability_service")


async def get_deliverability_checklist(listmonk: ListMonkClient) -> dict:
    """Fetch current Listmonk settings and run deliverability checks."""
    try:
        res = await listmonk.get_settings()
        raw_settings = res.get("data", {})
    except Exception as e:
        logger.error(f"Failed to fetch Listmonk settings: {e}")
        return {
            "score": 0,
            "overall_status": "error",
            "error": str(e),
            "checks": [],
        }

    checks = []

    # 1. Tracking pixel disabled
    disable_tracking = raw_settings.get("privacy.disable_tracking")
    checks.append({
        "id": "disable_tracking",
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
        "name": "Public Archives Disabled",
        "description": "Public campaign pages expose outreach copy to scrapers and domain blacklists.",
        "status": "pass" if public_archive is False else "info",
        "current_value": public_archive,
        "recommended": False,
        "impact": "Low",
    })

    # Calculate score
    total_checks = len(checks)
    passed = sum(1 for c in checks if c["status"] == "pass")
    score = int((passed / total_checks) * 100)

    overall_status = "optimal" if score == 100 else ("good" if score >= 70 else "needs_attention")

    return {
        "score": score,
        "overall_status": overall_status,
        "checks": checks,
        "raw_settings": {
            "disable_tracking": disable_tracking,
            "individual_tracking": ind_tracking,
            "unsubscribe_header": unsub_header,
            "batch_size": batch_size,
            "concurrency": concurrency,
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
        await listmonk.update_settings(payload)
        logger.info(f"Applied {mode} preset settings to Listmonk")
        return {"status": "ok", "mode": mode, "applied": payload}
    except Exception as e:
        logger.error(f"Failed to apply {mode} preset: {e}")
        raise
