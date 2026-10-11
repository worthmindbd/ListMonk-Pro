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
from app.services.imap_helpers import safe_email_for_query, extract_email_body
from app.services.imap_unsubscribe import _extract_reply_only, _match_campaign
from app.services.reply_log import (
    append_log, load_processed_message_ids, mark_processed_message_ids,
)

logger = logging.getLogger("imap_reply_tracker")

# Campaign history walks back this far (bounded) so a reply can be matched to
# the campaign that actually targeted the sender, not just the newest one.
MAX_CAMPAIGN_PAGES = 20
CAMPAIGN_PAGE_SIZE = 100

# Serialises reply scans so the 15-minute background loop and a manual
# "Scan Mailbox" click cannot process (and log) the same message twice.
_reply_scan_lock = asyncio.Lock()

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


def _campaign_created_at(camp: dict) -> Optional[datetime]:
    """Parse a campaign's ``created_at`` into an aware UTC datetime."""
    created = camp.get("created_at") or ""
    if not created:
        return None
    for candidate in (created, created[:10]):
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


async def _fetch_campaigns_covering(
    listmonk: ListMonkClient, cutoff: datetime,
) -> tuple[list[dict], bool]:
    """Fetch campaigns newest-first, walking back until ``cutoff`` is covered.

    Returns ``(campaigns, complete)``; ``complete`` is False when the page cap
    was hit first, meaning campaigns older than the returned set are missing.
    """
    campaigns: list[dict] = []
    oldest: Optional[datetime] = None
    page = 1

    while True:
        res = await listmonk.get_campaigns(
            page=page, per_page=CAMPAIGN_PAGE_SIZE,
            order_by="created_at", order="DESC",
        )
        results = (res.get("data") or {}).get("results") or []
        if not results:
            return campaigns, True

        campaigns.extend(results)
        for c in results:
            dt = _campaign_created_at(c)
            if dt is not None and (oldest is None or dt < oldest):
                oldest = dt

        if oldest is not None and oldest <= cutoff:
            return campaigns, True
        if page >= MAX_CAMPAIGN_PAGES:
            return campaigns, False
        page += 1


def _subject_matches(clean_subj: str, campaign: dict) -> bool:
    """True when a campaign's subject/name matches the cleaned reply subject."""
    if not clean_subj:
        return False
    c_subj = (campaign.get("subject") or "").strip().lower()
    c_name = (campaign.get("name") or "").strip().lower()
    if c_subj and (clean_subj == c_subj or c_subj in clean_subj):
        return True
    # Campaign names often mirror the subject; require a meaningful overlap so
    # a short reply subject cannot match an unrelated campaign by substring.
    return bool(c_name) and len(clean_subj) >= 8 and clean_subj in c_name


async def match_campaign_for_reply(
    listmonk: ListMonkClient,
    subject: str,
    from_email: str,
    reply_date: Optional[datetime] = None,
    campaigns: Optional[list[dict]] = None,
) -> tuple[Optional[int], Optional[str]]:
    """Find the campaign a prospect reply belongs to.

    Matching order:
      1. Subject match against the campaign history (walked back to cover the
         reply date, bounded by MAX_CAMPAIGN_PAGES).
      2. List-aware match: the newest campaign created on or before the reply
         that targeted one of the sender's lists.

    Recency alone is never used — attributing a reply to an unrelated campaign
    is worse than leaving it unattributed. Pass a pre-fetched ``campaigns``
    list to avoid re-walking the history per message.
    """
    clean_subj = _clean_subject_for_matching(subject)
    if reply_date is None:
        reply_date = datetime.now(timezone.utc)

    if campaigns is None:
        try:
            campaigns, complete = await _fetch_campaigns_covering(listmonk, reply_date)
            if not complete:
                logger.warning(
                    "[ReplyTracker] Campaign history truncated at %d pages while "
                    "matching a reply", MAX_CAMPAIGN_PAGES,
                )
        except Exception as e:
            logger.debug(f"Campaign lookup error: {e}")
            return None, None

    if not campaigns:
        return None, None

    for c in campaigns:
        if _subject_matches(clean_subj, c):
            return c.get("id"), c.get("name")

    # Fall back to list-aware matching against the sender's lists.
    safe_email = safe_email_for_query(from_email)
    if not safe_email:
        return None, None
    try:
        res = await listmonk.get_subscribers(
            1, 1, f"subscribers.email = '{safe_email}'",
        )
        subs = (res.get("data") or {}).get("results") or []
    except Exception as e:
        logger.debug(f"Subscriber lookup error: {e}")
        return None, None
    if not subs:
        return None, None

    sub_list_ids = {
        l.get("id") for l in (subs[0].get("lists") or []) if l.get("id")
    }
    match = _match_campaign(campaigns, reply_date, sub_list_ids)
    if match:
        return match.get("campaign_id"), match.get("campaign_name")
    return None, None


def extract_clean_reply(full_body: str) -> str:
    """Extract clean prospect reply text, stripping quoted threads.

    Delegates to the shared quote stripper so the reply feed and the
    unsubscribe monitor agree on what counts as the prospect's own text. If
    the entire body looks quoted, keep a short prefix for display instead of
    showing nothing.
    """
    if not full_body:
        return ""
    cleaned = _extract_reply_only(full_body)
    if cleaned:
        return cleaned
    return full_body[:500].strip()


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
    """Connect to IMAP and scan for prospect replies.

    Serialised with a module lock: the 15-minute background loop and the
    manual "Scan Mailbox" endpoint both call this, and two overlapping scans
    would load the same processed-ID set and log duplicate replies.
    """
    if _reply_scan_lock.locked():
        return {
            "error": None,
            "scanned": 0,
            "new_replies": 0,
            "positive": 0,
            "negative": 0,
            "auto_reply": 0,
            "question": 0,
            "message": "Reply scan already in progress",
        }

    async with _reply_scan_lock:
        return await _scan_and_track_replies_impl(listmonk, days_back)


async def _scan_and_track_replies_impl(
    listmonk: ListMonkClient,
    days_back: int = 14
) -> dict:
    """Body of the reply scan; callers must hold ``_reply_scan_lock``."""
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
    new_msg_ids: list[str] = []
    stats = {
        "scanned": 0,
        "new_replies": 0,
        "positive": 0,
        "negative": 0,
        "auto_reply": 0,
        "question": 0,
    }

    # Fetch the campaign history once per scan, walked back far enough to
    # cover every reply in the scan window. A per-message fetch (or a single
    # page) both mis-attributes old replies and hammers the API.
    try:
        campaigns, campaigns_complete = await _fetch_campaigns_covering(
            listmonk, datetime.now(timezone.utc) - timedelta(days=days_back),
        )
        if not campaigns_complete:
            logger.warning(
                "[ReplyTracker] Campaign history truncated at %d pages; older "
                "replies may be left unattributed", MAX_CAMPAIGN_PAGES,
            )
    except Exception as e:
        logger.warning(f"[ReplyTracker] Could not fetch campaigns: {e}")
        campaigns = []

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
                new_msg_ids.append(msg_id)
            continue

        to_header = msg.get("To", "")
        _, to_email = email.utils.parseaddr(to_header)
        subject = msg.get("Subject", "")
        date_str = msg.get("Date", "")

        # Parse the reply's own date so campaign matching can prefer campaigns
        # that already existed when the prospect replied.
        reply_date = None
        if date_str:
            try:
                dt = email.utils.parsedate_to_datetime(date_str)
                reply_date = (
                    dt.astimezone(timezone.utc) if dt.tzinfo
                    else dt.replace(tzinfo=timezone.utc)
                )
            except (ValueError, TypeError):
                reply_date = None

        # Extract stripped reply content
        full_body = extract_email_body(msg)
        reply_only = extract_clean_reply(full_body)

        sentiment = classify_sentiment(subject, reply_only)

        # Match campaign (history was fetched once for the whole scan)
        camp_id, camp_name = await match_campaign_for_reply(
            listmonk, subject, from_email, reply_date, campaigns=campaigns,
        )

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
            new_msg_ids.append(msg_id)

    if new_records:
        await append_log(new_records)
        logger.info(f"IMAP Reply Tracker: Recorded {len(new_records)} new prospect replies")

    # One read+write for the whole scan instead of one per message.
    if new_msg_ids:
        mark_processed_message_ids(new_msg_ids)

    return stats
