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
import hashlib
import imaplib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import settings
from app.services.bounce_filters import subscriber_opened_campaign
from app.services.listmonk_client import ListMonkClient

logger = logging.getLogger("bounce_ingest")

from app.services.imap_helpers import (
    safe_email_for_query, imap_date, extract_email_body, render_container_text,
)


# ─── Processed-message state ─────────────────────────────────────────────────
#
# The bounce mailbox is an ordinary mailbox that a human also reads, so IMAP
# \Seen alone cannot mean "already ingested": opening a DSN in webmail would
# hide it from every future scan. A persisted ID set makes ingestion
# idempotent regardless of read state (mirrors reply_processed.json and
# unsubscribe_processed.json).
PROCESSED_FILE = settings.data_path("bounce_processed.json")
MAX_PROCESSED_IDS = 5000

# DSN "Action:" values (RFC 3464) that are not bounces at all. A delay notice
# means the message is still in flight, so recording a bounce for it would be
# wrong — the eventual failure arrives as its own DSN with its own Message-ID.
_NON_BOUNCE_ACTIONS = ("delayed",)


def _message_state_id(msg: email.message.EmailMessage) -> str:
    """Stable identifier for a DSN, preferring the Message-ID header."""
    message_id = (msg.get("Message-ID") or "").strip()
    if message_id:
        return message_id
    fingerprint = "|".join([
        str(msg.get("From") or ""),
        str(msg.get("Date") or ""),
        str(msg.get("Subject") or ""),
    ])
    return "fp:" + hashlib.sha256(fingerprint.encode("utf-8", "replace")).hexdigest()[:32]


def _dsn_action(body: str) -> str:
    """Return the RFC 3464 ``Action:`` value of a DSN body, if present."""
    match = re.search(r"^\s*Action:\s*(.+)$", body, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip().lower() if match else ""


def load_processed_ids() -> list[str]:
    """Return persisted handled-DSN identifiers, oldest first."""
    try:
        data = json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data if x]
    except (FileNotFoundError, json.JSONDecodeError, IsADirectoryError, PermissionError):
        pass
    return []


def save_processed_ids(ids: list[str]) -> None:
    """Persist handled-DSN identifiers atomically, capped to the newest N."""
    trimmed = ids[-MAX_PROCESSED_IDS:] if len(ids) > MAX_PROCESSED_IDS else ids
    tmp_path = None
    try:
        PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
        # NamedTemporaryFile creates the file 0600 and os.replace is atomic, so
        # the state file never appears world-readable or half-written (the other
        # state files in DATA_DIR are 0600 too).
        with tempfile.NamedTemporaryFile(
            "w", dir=PROCESSED_FILE.parent, prefix=PROCESSED_FILE.name, suffix=".tmp",
            delete=False, encoding="utf-8",
        ) as f:
            tmp_path = f.name
            json.dump(trimmed, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, PROCESSED_FILE)
    except OSError as e:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        logger.warning(f"Could not persist processed bounce ids: {e}")


def mark_processed_ids(ids: list[str]) -> None:
    """Add identifiers to the processed set in one read + one write."""
    fresh = [str(i) for i in ids if i]
    if not fresh:
        return
    current = load_processed_ids()
    known = set(current)
    added = [i for i in fresh if i not in known]
    if not added:
        return
    current.extend(added)
    save_processed_ids(current)


def _fetch_state_id(conn: imaplib.IMAP4_SSL, msg_id: bytes) -> str:
    """Fetch only the Message-ID header so processed DSNs need no full download."""
    try:
        status, data = conn.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
    except Exception as e:
        logger.warning(f"header fetch failed for {msg_id!r}: {e}")
        return ""
    if status != "OK" or not data:
        return ""
    raw = b""
    for part in data:
        if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], (bytes, bytearray)):
            raw = bytes(part[1])
            break
    if not raw:
        return ""
    return _message_state_id(email.message_from_bytes(raw, policy=email.policy.default))


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
    elif "relay access denied" in lowered or "access denied" in lowered:
        # 5.7.x policy/relay rejection: the mailbox may be perfectly valid, the
        # receiving server just refuses to relay for us. Never blocklist on it.
        reason_tag = "soft: relay_denied"
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
                # The delivery-status part is a container: its fields live in
                # child messages, so get_payload(decode=True) is None and the
                # recipients would be invisible without expanding it.
                dsn_text = render_container_text(part)
                for line in dsn_text.splitlines():
                    if line.lower().startswith(("final-recipient:", "original-recipient:")):
                        match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", line)
                        if match:
                            return match.group(0).lower()

    own_addr = (settings.bounce_imap_user or "").lower()

    # Some relays emit the DSN as a plain text/plain message with the RFC 3464
    # fields inline instead of a message/delivery-status part. Those bodies
    # still carry "Final-Recipient: rfc822; user@domain", which the generic
    # patterns below cannot reach (the line has no angle brackets and the
    # address is not the first token after the colon).
    for line in body.splitlines():
        if line.lower().startswith(("final-recipient:", "original-recipient:")):
            match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", line)
            if match and match.group(0).lower() != own_addr:
                return match.group(0).lower()

    patterns = [
        re.compile(r"delivery\s+to\s+<?(\S+@\S+\.\w+)>?", re.IGNORECASE),
        re.compile(r"recipient[:\s]+<?(\S+@\S+\.\w+)>?", re.IGNORECASE),
        re.compile(r"<(\S+@\S+\.\w+)>", re.IGNORECASE),
    ]
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

# DSNs from the last BOUNCE_SCAN_DAYS days are scanned, so campaign
# attribution must be able to cover the same window. A single page of the 50
# newest campaigns silently dropped bounces for anything older.
BOUNCE_SCAN_DAYS = 30
MAX_CAMPAIGN_PAGES = 20
CAMPAIGN_PAGE_SIZE = 100


def _parse_campaign_dt(created: str) -> Optional[datetime]:
    """Parse a ListMonk ``created_at`` string into an aware UTC datetime."""
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            dt = datetime.fromisoformat(created[:10])
        except (ValueError, TypeError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _fetch_campaigns_covering(
    client: ListMonkClient, cutoff: datetime,
) -> tuple[list[dict], bool]:
    """Fetch campaigns newest-first until ``cutoff`` is covered.

    Returns ``(campaigns, complete)``. ``complete`` is False when the page cap
    was reached before walking back to ``cutoff``, which means very old
    campaigns are missing from the list and attribution may skip their bounces.
    """
    campaigns: list[dict] = []
    oldest: Optional[datetime] = None
    page = 1

    while True:
        res = await client.get_campaigns(
            page=page, per_page=CAMPAIGN_PAGE_SIZE,
            order_by="created_at", order="DESC",
        )
        results = (res.get("data") or {}).get("results") or []
        if not results:
            return campaigns, True

        campaigns.extend(results)
        for c in results:
            dt = _parse_campaign_dt(c.get("created_at", ""))
            if dt is not None and (oldest is None or dt < oldest):
                oldest = dt

        if oldest is not None and oldest <= cutoff:
            return campaigns, True
        if page >= MAX_CAMPAIGN_PAGES:
            return campaigns, False
        page += 1


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
    Scan the bounce mailbox for DSNs in the scan window, classify each, and
    create a matching bounce record in ListMonk.

    Idempotency comes from the persisted processed-ID set (not IMAP \\Seen), so
    a DSN that a human already opened in webmail is still ingested exactly
    once. Messages are also marked \\Seen so the shared mailbox stays tidy.

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

        # Campaign history covering the whole DSN scan window, fetched once for
        # the fallback attribution.
        scan_since = datetime.now(timezone.utc) - timedelta(days=BOUNCE_SCAN_DAYS)
        campaigns_ok = True
        try:
            recent_campaigns, campaigns_complete = await _fetch_campaigns_covering(
                client, scan_since,
            )
            campaigns_ok = campaigns_complete
            if not campaigns_complete:
                logger.warning(
                    "[BounceIngest] Campaign history truncated after %d pages; "
                    "bounces for campaigns older than %s will be skipped",
                    MAX_CAMPAIGN_PAGES, scan_since.date(),
                )
        except Exception as e:
            logger.warning(f"Could not fetch recent campaigns: {e}")
            recent_campaigns = []
            campaigns_ok = False

        try:
            await asyncio.to_thread(conn.select, "INBOX", readonly=False)
            since = imap_date(scan_since)
            processed_ids = load_processed_ids()
            processed_set = set(processed_ids)
            handled_ids: list[str] = []

            status, msg_ids = await asyncio.to_thread(
                conn.search, None, f"(SINCE {since})"
            )
            if status != "OK" or not msg_ids or not msg_ids[0]:
                return {"scanned": 0, "ingested": 0, "skipped": 0, "errors": 0,
                        "message": f"No bounce emails since {since}"}

            candidates = [m for m in msg_ids[0].split() if m]
            logger.info(
                f"[BounceIngest] {len(candidates)} DSN(s) in the last "
                f"{BOUNCE_SCAN_DAYS} days ({len(processed_set)} already processed)"
            )

            async def _finish(msg_id: bytes, state_id: str) -> None:
                """Record a terminal outcome so this DSN is never re-processed."""
                if state_id:
                    handled_ids.append(state_id)
                    processed_set.add(state_id)
                try:
                    await asyncio.to_thread(conn.store, msg_id, "+FLAGS", "\\Seen")
                except Exception as e:
                    logger.warning(f"Could not mark {msg_id!r} as seen: {e}")

            for msg_id in candidates:
                try:
                    # Cheap header fetch first: a DSN that a human already
                    # opened (so it is no longer UNSEEN) must still be ingested
                    # exactly once, and one already handled must not be
                    # re-downloaded.
                    state_id = await asyncio.to_thread(_fetch_state_id, conn, msg_id)
                    if state_id and state_id in processed_set:
                        continue

                    status, data = await asyncio.to_thread(conn.fetch, msg_id, "(RFC822)")
                    if status != "OK" or not data or not data[0]:
                        errors += 1
                        continue

                    raw = data[0][1]
                    msg = email.message_from_bytes(raw, policy=email.policy.default)
                    subject = msg.get("Subject", "")
                    body = _extract_body(msg)
                    scanned += 1

                    # Delay notices (Action: delayed) are not bounces: the
                    # message is still in flight, and a later failure arrives
                    # as its own DSN.
                    dsn_action = _dsn_action(body)
                    if dsn_action in _NON_BOUNCE_ACTIONS:
                        skipped += 1
                        reason_key = f"not_a_bounce:{dsn_action}"
                        skipped_reasons[reason_key] = skipped_reasons.get(reason_key, 0) + 1
                        await _finish(msg_id, state_id)
                        continue

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
                        await _finish(msg_id, state_id)
                        continue

                    # Look up subscriber
                    safe_email = safe_email_for_query(recipient)
                    if not safe_email:
                        logger.warning(f"Invalid recipient format, skipping: {recipient}")
                        await _finish(msg_id, state_id)
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
                        await _finish(msg_id, state_id)
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
                        if campaigns_ok:
                            await _finish(msg_id, state_id)
                        else:
                            # Attribution needs the campaign list: without it we
                            # cannot tell "old campaign" from "fetch failed", and
                            # discarding the DSN would lose a real bounce for good.
                            # Leave it unprocessed so the next scan retries it.
                            logger.warning(
                                "[BounceIngest] campaign history unavailable, "
                                "will retry %s on the next scan", recipient,
                            )
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
                        await _finish(msg_id, state_id)
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
                        await _finish(msg_id, state_id)
                    except Exception as e:
                        logger.error(f"create_bounce failed for {recipient}: {e}")
                        errors += 1
                        # Do NOT mark seen — let a retry pick it up next scan

                except Exception as e:
                    logger.error(f"error processing message {msg_id}: {e}")
                    errors += 1

            # Persist every terminal outcome in one write (the reply scanner
            # does the same) so the next scan skips them without downloading.
            if handled_ids:
                await asyncio.to_thread(mark_processed_ids, handled_ids)

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
        camp_date = _parse_campaign_dt(camp.get("created_at", ""))
        if camp_date is not None and camp_date <= cutoff:
            return camp

    return None
