"""
Bounce ingestion: reads bounce DSN emails from the IMAP mailbox, classifies
each, and creates matching records in ListMonk.

Classification policy (best practice, matches SendGrid / Mailgun / Postmark /
AWS SES): only truly invalid addresses become HARD. Mailbox-full, blacklist,
reputation, rate limit, content, and 4xx failures are SOFT — ListMonk's
soft-bounce aggregation decides eventual blocklisting, not this ingester.

Only subscribers with HARD bounces get blocklisted (via ListMonk's own
threshold on hard bounces). This module never calls blocklist directly.
"""

import asyncio
import email
import email.policy
import email.utils
import imaplib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import settings
from app.services.bounce_filters import subscriber_opened_campaign
from app.services.listmonk_client import ListMonkClient

logger = logging.getLogger("bounce_ingest")

from app.services.imap_helpers import safe_email_for_query, imap_date, extract_email_body


# ─── Classification ──────────────────────────────────────────────────────────

# Enhanced status codes (RFC 3463) that indicate a permanently invalid
# recipient. Anything else is soft.
HARD_CODE_PATTERNS = [
    r"5\.1\.1\b",   # Bad destination mailbox address (user unknown)
    r"5\.1\.2\b",   # Bad destination system address (host/domain unknown)
    r"5\.1\.3\b",   # Bad destination mailbox address syntax
    r"5\.1\.10\b",  # Recipient address has null MX
    r"5\.2\.1\b",   # Mailbox disabled, not accepting messages
    r"5\.4\.4\b",   # Unable to route (permanent routing failure)
]

HARD_KEYWORD_PATTERNS = [
    r"user unknown",
    r"no such user",
    r"recipient address rejected:\s*user unknown",
    r"address does not exist",
    r"account (?:has been )?disabled",
    r"no mailbox here",
    r"unknown recipient",
    r"does not have an? account",
    r"email account that you tried to reach does not exist",
    r"relay access denied",
    r"no such (?:recipient|mailbox|address)",
]

HARD_PATTERN = re.compile(
    "|".join(HARD_CODE_PATTERNS + HARD_KEYWORD_PATTERNS),
    re.IGNORECASE,
)

# Best-effort extraction of the enhanced status code so we can surface it in
# bounce metadata for debugging.
ENHANCED_CODE_RE = re.compile(r"\b([245])\.\d{1,3}\.\d{1,3}\b")


def classify_bounce(body: str, subject: str = "") -> dict:
    """
    Classify a bounce DSN body/subject as hard or soft.

    Returns {"type": "hard"|"soft", "reason": str, "smtp_code": str|None}.
    Default is soft when nothing matches — never mark a subscriber permanently
    dead on ambiguous signals.
    """
    text = f"{subject}\n{body}"

    hard_match = HARD_PATTERN.search(text)
    if hard_match:
        matched = hard_match.group(0).strip()
        hard_code_match = ENHANCED_CODE_RE.match(matched)
        smtp_code = hard_code_match.group(0) if hard_code_match else None
        if not smtp_code:
            code_match = ENHANCED_CODE_RE.search(text)
            smtp_code = code_match.group(0) if code_match else None
        return {
            "type": "hard",
            "reason": f"hard: {matched.lower()}",
            "smtp_code": smtp_code,
        }

    code_match = ENHANCED_CODE_RE.search(text)
    smtp_code = code_match.group(0) if code_match else None

    # Soft with best-effort reason tag
    reason_tag = "soft: unknown"
    lowered = text.lower()
    if "mailbox" in lowered and "full" in lowered:
        reason_tag = "soft: mailbox_full"
    elif "quota" in lowered:
        reason_tag = "soft: quota_exceeded"
    elif any(kw in lowered for kw in ("spamhaus", "blacklist", "blocklist", "rbl", "dnsbl")):
        reason_tag = "soft: blacklist"
    elif any(kw in lowered for kw in ("reputation", "5.7.1")):
        reason_tag = "soft: reputation_policy"
    elif any(kw in lowered for kw in ("greylist", "try again", "try later", "temporary")):
        reason_tag = "soft: transient"
    elif "too large" in lowered or "size limit" in lowered or "5.2.3" in lowered:
        reason_tag = "soft: message_too_large"
    elif "content" in lowered and "reject" in lowered:
        reason_tag = "soft: content_rejected"
    elif smtp_code and smtp_code.startswith("4"):
        reason_tag = "soft: 4xx_transient"

    return {
        "type": "soft",
        "reason": reason_tag,
        "smtp_code": smtp_code,
    }


# ─── Parsing helpers ─────────────────────────────────────────────────────────

_BOUNCE_CONTENT_TYPES = ("text/plain", "message/delivery-status")


def _extract_body(msg: email.message.EmailMessage) -> str:
    return extract_email_body(msg, content_types=_BOUNCE_CONTENT_TYPES)


def _extract_bounced_recipient(msg: email.message.EmailMessage, body: str) -> Optional[str]:
    for header in ("X-Failed-Recipients", "X-Original-To",
                   "Original-Recipient", "Final-Recipient"):
        val = msg.get(header, "")
        if val:
            if ";" in val:
                val = val.split(";", 1)[1].strip()
            match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", val)
            if match:
                return match.group(0).lower()

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "message/delivery-status":
                payload = part.get_payload(decode=True)
                dsn_text = payload.decode("utf-8", errors="replace") if payload else str(part.get_payload())
                for line in dsn_text.splitlines():
                    if line.lower().startswith(("final-recipient:", "original-recipient:")):
                        match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", line)
                        if match:
                            return match.group(0).lower()

    patterns = [
        re.compile(r"delivery\s+to\s+<?(\S+@\S+\.\w+)>?", re.IGNORECASE),
        re.compile(r"recipient[:\s]+<?(\S+@\S+\.\w+)>?", re.IGNORECASE),
        re.compile(r"<(\S+@\S+\.\w+)>", re.IGNORECASE),
    ]
    own_addr = (settings.bounce_imap_user or "").lower()
    for pat in patterns:
        for m in pat.finditer(body):
            addr = m.group(1).lower().rstrip(">")
            if addr.startswith("mailto:"):
                addr = addr[len("mailto:"):]
            if addr and addr != own_addr:
                return addr
    return None


LISTMONK_CAMPAIGN_HEADERS = (
    "X-Listmonk-Campaign",
    "X-ListMonk-Campaign",
    "X-Listmonk-Campaign-UUID",
    "List-ID",
)


def _extract_campaign_hint(msg: email.message.EmailMessage) -> Optional[str]:
    """Look inside the attached original message for ListMonk campaign headers."""
    if not msg.is_multipart():
        return None
    for part in msg.walk():
        if part.get_content_type() in ("message/rfc822", "text/rfc822-headers"):
            inner_payload = part.get_payload()
            inner = inner_payload[0] if isinstance(inner_payload, list) and inner_payload else None
            if inner is None:
                continue
            for hdr in LISTMONK_CAMPAIGN_HEADERS:
                val = inner.get(hdr, "") if hasattr(inner, "get") else ""
                if val:
                    return str(val).strip()
    return None


# ─── Core ingestion ──────────────────────────────────────────────────────────

def connect_bounce_imap_rw() -> Optional[imaplib.IMAP4_SSL]:
    """Connect to the bounce IMAP mailbox in read-write mode (needed to mark seen)."""
    if not settings.bounce_imap_configured:
        logger.warning("Bounce IMAP not configured")
        return None
    try:
        if settings.bounce_imap_use_ssl:
            conn = imaplib.IMAP4_SSL(settings.bounce_imap_host, settings.bounce_imap_port)
        else:
            conn = imaplib.IMAP4(settings.bounce_imap_host, settings.bounce_imap_port)
        conn.login(settings.bounce_imap_user, settings.bounce_imap_pass)
        return conn
    except Exception as e:
        logger.error(f"Bounce IMAP connection failed: {e}")
        return None


_INGEST_LOCK = asyncio.Lock()


async def ingest_bounce_mailbox(client: ListMonkClient) -> dict:
    """
    Scan the bounce mailbox for UNSEEN bounces, classify each, and create a
    matching bounce record in ListMonk. Messages are marked \\Seen after
    processing so subsequent scans don't re-ingest them.

    Campaign attribution order:
      1. ListMonk campaign headers inside the attached original message
      2. Most recent campaign targeting any list the subscriber belongs to,
         created on or before the bounce timestamp
      3. Skip (can't attribute → don't create incomplete record)
    """
    if _INGEST_LOCK.locked():
        return {"scanned": 0, "ingested": 0, "skipped": 0, "errors": 0,
                "message": "Ingest already in progress"}

    async with _INGEST_LOCK:
        conn = await asyncio.to_thread(connect_bounce_imap_rw)
        if not conn:
            return {"scanned": 0, "ingested": 0, "skipped": 0, "errors": 0,
                    "message": "Bounce IMAP not configured or connection failed"}

        scanned = 0
        ingested = 0
        hard_count = 0
        soft_count = 0
        skipped = 0
        errors = 0
        skipped_reasons: dict = {}

        # Cache recent campaigns once for the fallback attribution.
        try:
            camp_res = await client.get_campaigns(
                page=1, per_page=50, order_by="created_at", order="DESC",
            )
            recent_campaigns = camp_res.get("data", {}).get("results", [])
        except Exception as e:
            logger.warning(f"Could not fetch recent campaigns: {e}")
            recent_campaigns = []

        try:
            await asyncio.to_thread(conn.select, "INBOX", readonly=False)
            since = imap_date(datetime.now(timezone.utc) - timedelta(days=30))
            status, msg_ids = await asyncio.to_thread(
                conn.search, None, f"(UNSEEN SINCE {since})"
            )
            if status != "OK" or not msg_ids or not msg_ids[0]:
                return {"scanned": 0, "ingested": 0, "skipped": 0, "errors": 0,
                        "message": "No unseen bounce emails"}

            ids = msg_ids[0].split()
            scanned = len(ids)
            logger.info(f"[BounceIngest] scanning {scanned} unseen bounce emails")

            for msg_id in ids:
                try:
                    status, data = await asyncio.to_thread(conn.fetch, msg_id, "(RFC822)")
                    if status != "OK" or not data or not data[0]:
                        errors += 1
                        continue

                    raw = data[0][1]
                    msg = email.message_from_bytes(raw, policy=email.policy.default)
                    subject = msg.get("Subject", "")
                    body = _extract_body(msg)

                    # Parse bounce date from the email Date header
                    bounce_date = None
                    date_str = msg.get("Date", "")
                    if date_str:
                        try:
                            dt = email.utils.parsedate_to_datetime(date_str)
                            if dt.tzinfo is not None:
                                bounce_date = dt.astimezone(timezone.utc)
                            else:
                                bounce_date = dt.replace(tzinfo=timezone.utc)
                        except (ValueError, TypeError, AttributeError):
                            bounce_date = None

                    recipient = _extract_bounced_recipient(msg, body)
                    if not recipient:
                        skipped += 1
                        skipped_reasons["no_recipient"] = skipped_reasons.get("no_recipient", 0) + 1
                        await asyncio.to_thread(conn.store, msg_id, "+FLAGS", "\\Seen")
                        continue

                    # Look up subscriber
                    safe_email = safe_email_for_query(recipient)
                    if not safe_email:
                        logger.warning(f"Invalid recipient format, skipping: {recipient}")
                        await asyncio.to_thread(conn.store, msg_id, "+FLAGS", "\\Seen")
                        continue
                    try:
                        sub_res = await client.get_subscribers(
                            1, 1, f"subscribers.email = '{safe_email}'"
                        )
                        subs = sub_res.get("data", {}).get("results", [])
                    except Exception as e:
                        logger.warning(f"subscriber lookup failed for {recipient}: {e}")
                        errors += 1
                        continue

                    if not subs:
                        skipped += 1
                        skipped_reasons["no_subscriber"] = skipped_reasons.get("no_subscriber", 0) + 1
                        await asyncio.to_thread(conn.store, msg_id, "+FLAGS", "\\Seen")
                        continue

                    subscriber = subs[0]
                    sub_list_ids = {l.get("id") for l in subscriber.get("lists", []) if l.get("id")}

                    # Campaign attribution
                    campaign = _pick_campaign(
                        msg,
                        subscriber_list_ids=sub_list_ids,
                        recent_campaigns=recent_campaigns,
                        bounce_date=bounce_date,
                    )
                    if not campaign:
                        skipped += 1
                        skipped_reasons["no_campaign"] = skipped_reasons.get("no_campaign", 0) + 1
                        await asyncio.to_thread(conn.store, msg_id, "+FLAGS", "\\Seen")
                        continue

                    if await subscriber_opened_campaign(
                        client, subscriber["id"], campaign["id"],
                    ):
                        skipped += 1
                        skipped_reasons["opened_campaign"] = (
                            skipped_reasons.get("opened_campaign", 0) + 1
                        )
                        logger.info(
                            f"[BounceIngest] skip {recipient}: opened campaign "
                            f"{campaign.get('id')}"
                        )
                        await asyncio.to_thread(conn.store, msg_id, "+FLAGS", "\\Seen")
                        continue

                    classification = classify_bounce(body, subject)
                    bounce_type = classification["type"]

                    try:
                        await client.create_bounce(
                            email=recipient,
                            campaign_uuid=campaign.get("uuid"),
                            bounce_type=bounce_type,
                            source="api",
                            meta={
                                "ingested_by": "bounce_ingest",
                                "classify_reason": classification["reason"],
                                "smtp_code": classification["smtp_code"],
                                "subject": subject[:200],
                            },
                        )
                        ingested += 1
                        if bounce_type == "hard":
                            hard_count += 1
                        else:
                            soft_count += 1
                        await asyncio.to_thread(conn.store, msg_id, "+FLAGS", "\\Seen")
                    except Exception as e:
                        logger.error(f"create_bounce failed for {recipient}: {e}")
                        errors += 1
                        # Do NOT mark seen — let a retry pick it up next scan

                except Exception as e:
                    logger.error(f"error processing message {msg_id}: {e}")
                    errors += 1

        finally:
            try:
                await asyncio.to_thread(conn.logout)
            except Exception:
                pass

        return {
            "scanned": scanned,
            "ingested": ingested,
            "hard": hard_count,
            "soft": soft_count,
            "skipped": skipped,
            "skipped_reasons": skipped_reasons,
            "errors": errors,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": f"Ingested {ingested} ({hard_count} hard, {soft_count} soft), skipped {skipped}, errors {errors}",
        }


def _pick_campaign(
    msg: email.message.EmailMessage,
    subscriber_list_ids: set,
    recent_campaigns: list,
    bounce_date: Optional[datetime] = None,
) -> Optional[dict]:
    """
    Pick the campaign this bounce belongs to:
      1. Campaign UUID/id extracted from attached original message headers
      2. Most recent campaign targeting any of subscriber's lists
         that was created on or before the bounce date.
    Returns the campaign dict or None.
    """
    hint = _extract_campaign_hint(msg)
    if hint:
        for camp in recent_campaigns:
            if str(camp.get("uuid", "")) == hint or str(camp.get("id", "")) == hint:
                return camp

    # Fall back to current time when bounce date is unavailable
    cutoff = bounce_date if bounce_date is not None else datetime.now(timezone.utc)
    for camp in recent_campaigns:
        camp_list_ids = [l.get("id") for l in (camp.get("lists") or [])]
        if not camp_list_ids:
            continue
        if not any(lid in subscriber_list_ids for lid in camp_list_ids):
            continue
        created = camp.get("created_at", "")
        if not created:
            continue
        try:
            clean_iso = created.replace("Z", "+00:00")
            camp_date = datetime.fromisoformat(clean_iso)
            if camp_date.tzinfo is None:
                camp_date = camp_date.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            try:
                camp_date = datetime.fromisoformat(created[:10]).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
        if camp_date <= cutoff:
            return camp

    return None
