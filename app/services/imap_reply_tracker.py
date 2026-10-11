"""IMAP Reply Tracker for ListMonk Pro Cold Outreach.

Scans the IMAP inbox for replies from cold email prospects, strips quoted
threads, categorizes sentiment (positive, question, auto_reply, negative),
and links responses to specific campaigns and subscribers.
"""

import asyncio
import email
import email.policy
import email.utils
import imaplib
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import settings
from app.services.listmonk_client import ListMonkClient
from app.services.imap_helpers import safe_email_for_query, imap_date, extract_email_body
from app.services.imap_unsubscribe import _extract_reply_only
from app.services.reply_log import (
    load_log, append_log, load_processed_message_ids, mark_processed_message_id,
    load_settings, update_reply_status
)

logger = logging.getLogger("imap_reply_tracker")

# Sentiment detection patterns
_OOO_PATTERNS = [
    re.compile(r"out\s+of\s+(the\s+)?office", re.IGNORECASE),
    re.compile(r"auto(matic)?\s*(-|\s)?\s*reply", re.IGNORECASE),
    re.compile(r"away\s+from\s+(my\s+)?desk", re.IGNORECASE),
    re.compile(r"on\s+(annual\s+)?leave", re.IGNORECASE),
    re.compile(r"on\s+vacation", re.IGNORECASE),
    re.compile(r"undeliverable", re.IGNORECASE),
    re.compile(r"delivery\s+status\s+notification", re.IGNORECASE),
    re.compile(r"mailbox\s+is\s+full", re.IGNORECASE),
]

_NEGATIVE_PATTERNS = [
    re.compile(r"not\s+interested", re.IGNORECASE),
    re.compile(r"unsubscribe", re.IGNORECASE),
    re.compile(r"remove\s+me", re.IGNORECASE),
    re.compile(r"take\s+me\s+off", re.IGNORECASE),
    re.compile(r"stop\s+emailing", re.IGNORECASE),
    re.compile(r"do\s+not\s+(email|contact)", re.IGNORECASE),
    re.compile(r"no\s+thanks", re.IGNORECASE),
    re.compile(r"wrong\s+person", re.IGNORECASE),
    re.compile(r"cease\s+and\s+desist", re.IGNORECASE),
    re.compile(r"report\s+(as\s+)?spam", re.IGNORECASE),
]

_POSITIVE_PATTERNS = [
    re.compile(r"sounds\s+good", re.IGNORECASE),
    re.compile(r"interested", re.IGNORECASE),
    re.compile(r"let'?s\s+(talk|chat|connect|meet|hop\s+on)", re.IGNORECASE),
    re.compile(r"send\s+(me\s+)?(over\s+)?(more\s+)?(info|information|details|pricing)", re.IGNORECASE),
    re.compile(r"schedule\s+(a\s+)?(call|time|demo)", re.IGNORECASE),
    re.compile(r"calendar|calendly", re.IGNORECASE),
    re.compile(r"pricing|how\s+much|cost", re.IGNORECASE),
    re.compile(r"yes(\s+please)?", re.IGNORECASE),
    re.compile(r"happy\s+to\s+connect", re.IGNORECASE),
    re.compile(r"tell\s+me\s+more", re.IGNORECASE),
    re.compile(r"give\s+me\s+a\s+call", re.IGNORECASE),
    re.compile(r"forwarded\s+to\s+our", re.IGNORECASE),
]


def classify_sentiment(subject: str, reply_text: str) -> str:
    """Classify the sentiment of a prospect reply.

    Returns: 'positive' | 'question' | 'auto_reply' | 'negative'
    """
    combined = f"{subject}\n{reply_text}"

    # 1. Out of Office / Auto-reply check
    for pat in _OOO_PATTERNS:
        if pat.search(combined):
            return "auto_reply"

    # 2. Negative / Unsubscribe check
    for pat in _NEGATIVE_PATTERNS:
        if pat.search(reply_text) or pat.search(subject):
            return "negative"

    # 3. Positive / Warm Lead check
    for pat in _POSITIVE_PATTERNS:
        if pat.search(reply_text):
            return "positive"

    # 4. Question check
    if "?" in reply_text or re.search(r"\b(how|what|when|who|where|why|can you|could you)\b", reply_text, re.IGNORECASE):
        return "question"

    # Default to question if short conversational reply, otherwise positive if polite
    if len(reply_text.strip()) > 0 and len(reply_text.strip()) < 150:
        return "question"

    return "question"


def _clean_subject_for_matching(subj: str) -> str:
    """Strip 'Re:', 'Fwd:', and whitespace for campaign subject matching."""
    cleaned = re.sub(r"^(re|fwd|fw):\s*", "", subj, flags=re.IGNORECASE).strip()
    return cleaned.lower()


async def match_campaign_for_reply(
    listmonk: ListMonkClient,
    subject: str,
    from_email: str
) -> tuple[Optional[int], Optional[str]]:
    """Try to find the campaign this reply belongs to by subject and recipient."""
    clean_subj = _clean_subject_for_matching(subject)

    try:
        camp_res = await listmonk.get_campaigns(page=1, per_page=100)
        campaigns = camp_res.get("data", {}).get("results", [])

        # Match by subject line
        for c in campaigns:
            c_subj = (c.get("subject") or "").strip().lower()
            c_name = (c.get("name") or "").strip().lower()
            if clean_subj and (clean_subj == c_subj or clean_subj in c_name or c_subj in clean_subj):
                return c.get("id"), c.get("name")

        # Fallback: check most recent running/finished campaign
        if campaigns:
            return campaigns[0].get("id"), campaigns[0].get("name")
    except Exception as e:
        logger.debug(f"Campaign lookup error: {e}")

    return None, None


def extract_clean_reply(full_body: str) -> str:
    """Extract clean prospect reply text, stripping quoted threads including multi-line 'On ... wrote:'."""
    if not full_body:
        return ""
    # Cut off at multi-line quote headers
    cleaned = re.split(r'\n\s*On\s+[\s\S]+?wrote:\s*\n?', full_body, flags=re.IGNORECASE)[0]
    cleaned = re.split(r'\n\s*-{2,}\s*Original Message\s*-{2,}', cleaned, flags=re.IGNORECASE)[0]
    cleaned = re.split(r'\n\s*From:\s+.+@.+\n\s*Sent:\s+', cleaned, flags=re.IGNORECASE)[0]

    lines = cleaned.splitlines()
    res = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if re.match(r'^On\s+.+wrote:\s*$', stripped, re.IGNORECASE):
            break
        res.append(line)

    final_text = "\n".join(res).strip()
    return final_text if final_text else full_body[:500].strip()


def _connect_imap() -> imaplib.IMAP4:
    if settings.imap_use_ssl:
        client = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    else:
        client = imaplib.IMAP4(settings.imap_host, settings.imap_port)
    client.login(settings.imap_user, settings.imap_pass)
    return client


async def scan_and_track_replies(
    listmonk: ListMonkClient,
    days_back: int = 14
) -> dict:
    """Connect to IMAP and scan for prospect replies."""
    if not settings.imap_configured:
        return {
            "error": "IMAP not configured in environment variables",
            "scanned": 0,
            "new_replies": 0,
            "positive": 0,
            "negative": 0,
            "auto_reply": 0,
            "question": 0,
        }

    processed_ids = load_processed_message_ids()
    new_records = []
    stats = {
        "scanned": 0,
        "new_replies": 0,
        "positive": 0,
        "negative": 0,
        "auto_reply": 0,
        "question": 0,
    }

    def _fetch_messages_sync():
        mail = _connect_imap()
        try:
            status, _ = mail.select("INBOX", readonly=True)
            if status != "OK":
                return []

            since = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%d-%b-%Y")
            status, search_data = mail.search(None, f'(SINCE "{since}")')
            if status != "OK" or not search_data[0]:
                return []

            msg_nums = search_data[0].split()
            # Most recent 200 messages
            msg_nums = msg_nums[-200:]
            messages = []
            for num in msg_nums:
                status, data = mail.fetch(num, "(RFC822)")
                if status == "OK" and data and data[0]:
                    raw = data[0][1]
                    msg = email.message_from_bytes(raw, policy=email.policy.default)
                    messages.append(msg)
            return messages
        finally:
            try:
                mail.close()
            except Exception:
                pass
            try:
                mail.logout()
            except Exception:
                pass

    try:
        messages = await asyncio.to_thread(_fetch_messages_sync)
    except Exception as e:
        logger.error(f"IMAP connection or fetch failed: {e}")
        return {"error": str(e), **stats}

    my_email = (settings.imap_user or "").strip().lower()

    for msg in messages:
        stats["scanned"] += 1
        msg_id = (msg.get("Message-ID") or "").strip()
        if msg_id and msg_id in processed_ids:
            continue

        from_header = msg.get("From", "")
        realname, from_email = email.utils.parseaddr(from_header)
        from_email = from_email.strip().lower()

        # Skip our own outgoing emails or empty addresses
        if not from_email or from_email == my_email:
            if msg_id:
                mark_processed_message_id(msg_id)
            continue

        to_header = msg.get("To", "")
        _, to_email = email.utils.parseaddr(to_header)
        subject = msg.get("Subject", "")
        date_str = msg.get("Date", "")

        # Extract stripped reply content
        full_body = extract_email_body(msg)
        reply_only = extract_clean_reply(full_body)

        sentiment = classify_sentiment(subject, reply_only)

        # Match campaign
        camp_id, camp_name = await match_campaign_for_reply(listmonk, subject, from_email)

        record = {
            "id": f"rep_{uuid.uuid4().hex[:10]}",
            "message_id": msg_id,
            "from_email": from_email,
            "from_name": realname or from_email.split("@")[0],
            "to_email": to_email or my_email,
            "subject": subject,
            "date": date_str,
            "body_preview": reply_only[:200] + ("..." if len(reply_only) > 200 else ""),
            "full_body": reply_only,
            "campaign_id": camp_id,
            "campaign_name": camp_name or "Cold Outreach Campaign",
            "sentiment": sentiment,
            "status": "lead" if sentiment == "positive" else "unread",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        new_records.append(record)
        stats["new_replies"] += 1
        stats[sentiment] = stats.get(sentiment, 0) + 1

        if msg_id:
            mark_processed_message_id(msg_id)

    if new_records:
        await append_log(new_records)
        logger.info(f"IMAP Reply Tracker: Recorded {len(new_records)} new prospect replies")

    return stats
