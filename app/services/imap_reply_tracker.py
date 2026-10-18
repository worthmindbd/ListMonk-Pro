"""IMAP Reply Tracker for ListMonk Pro Cold Outreach.

Scans the IMAP inbox for replies from cold email prospects, strips quoted
threads, categorizes sentiment (positive, auto_reply, negative, bounce),
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
from app.services.imap_helpers import safe_email_for_query, extract_email_body, imap_date
from app.services.imap_unsubscribe import _extract_reply_only, _match_campaign
# Shared idempotency-key helper: Message-ID when present, otherwise a
# From|Date|Subject fingerprint (same scheme as the bounce ingester).
# DSNs caught in the reply inbox are ingested through the same bounce
# pipeline as bounce-mailbox DSNs, sharing its processed-ID set.
from app.services.bounce_ingest import (
    _INGEST_LOCK,
    _message_state_id, _extract_body, ingest_parsed_dsn, mark_processed_ids,
    load_processed_ids as load_processed_bounce_ids,
)
from app.services.reply_log import (
    append_log, load_processed_message_ids, mark_processed_message_ids,
    load_log, save_log, _log_lock,
)

logger = logging.getLogger("imap_reply_tracker")

# Campaign history walks back this far (bounded) so a reply can be matched to
# the campaign that actually targeted the sender, not just the newest one.
MAX_CAMPAIGN_PAGES = 20
CAMPAIGN_PAGE_SIZE = 100

# Serialises reply scans so the 15-minute background loop and a manual
# "Scan Mailbox" click cannot process (and log) the same message twice.
_reply_scan_lock = asyncio.Lock()

# Sentiment detection patterns — automated / non-human responses.
# Covers out-of-office notices, ticket-system auto-responses (Zendesk,
# Freshdesk, etc.), generic "we received your message" auto-replies, and
# common non-English OOO phrases. These are never genuine prospect replies
# and must not inflate the reply rate or receive campaign attribution.
_OOO_PATTERNS = [
    # ── English OOO / away ──────────────────────────────────────────────
    re.compile(r"out\s+of\s+(the\s+)?office", re.IGNORECASE),
    re.compile(r"auto(matic)?\s*(-|\s)?\s*reply", re.IGNORECASE),
    re.compile(r"away\s+from\s+(my\s+)?desk", re.IGNORECASE),
    re.compile(r"on\s+(annual\s+)?leave", re.IGNORECASE),
    re.compile(r"on\s+vacation", re.IGNORECASE),
    re.compile(r"mailbox\s+is\s+full", re.IGNORECASE),
    # ── Non-English OOO (common cold-outreach locales) ──────────────────
    re.compile(r"fuera\s+de\s+oficina", re.IGNORECASE),          # Spanish
    re.compile(r"aus\s+dem\s+büro|außer\s+haus", re.IGNORECASE),  # German
    re.compile(r"absent\s+du\s+bureau", re.IGNORECASE),           # French
    re.compile(r"fora\s+do\s+escritório", re.IGNORECASE),         # Portuguese
    re.compile(r"不在办公室|休假中", re.IGNORECASE),                # Chinese
    # ── Ticket-system auto-responses ────────────────────────────────────
    re.compile(r"request\s+(has\s+been\s+)?received", re.IGNORECASE),
    re.compile(r"ticket\s+(created|number|#[A-Z0-9])", re.IGNORECASE),
    re.compile(r"your\s+(message|request|inquiry)\s+has\s+been\s+received", re.IGNORECASE),
    re.compile(r"being\s+reviewed\s+by\s+(the\s+)?.+\s*(team|support|care)", re.IGNORECASE),
    re.compile(r"we'?ll\s+(get\s+back\s+to\s+you|be\s+in\s+touch)\s+(within|as\s+soon)", re.IGNORECASE),
    re.compile(r"a\s+team\s+member\s+will\s+be\s+in\s+touch", re.IGNORECASE),
    re.compile(r"zendesk|freshdesk|help\s+desk\s+ticket", re.IGNORECASE),
    # ── Generic auto-reply / acknowledgement ────────────────────────────
    re.compile(r"we\s+have\s+received\s+your\s+message", re.IGNORECASE),
    re.compile(r"working\s+quickly\s+to\s+respond", re.IGNORECASE),
    re.compile(r"this\s+is\s+an\s+automated\s+(response|message|reply)", re.IGNORECASE),
    re.compile(r"do\s+not\s+reply\s+to\s+this\s+(email|message)", re.IGNORECASE),
    re.compile(r"this\s+email\s+was\s+sent\s+from\s+a\s+notification", re.IGNORECASE),
]

# Bounce / DSN detection, checked before every sentiment keyword. Mailer-daemon
# notices land in the same inbox as prospect replies, and a DSN matched none of
# the keyword lists below — so the unknown-means-positive default flagged a
# hard bounce ("550-5.1.1 The email account that you tried to reach does not
# exist") as a Hot Lead. Gmail's wording ("Mail delivery failed: returning
# message to sender") differs per provider, so both subject and body markers
# from the common senders (Gmail, Outlook, Exim, qmail, Postfix) are covered.
_BOUNCE_PATTERNS = [
    re.compile(r"mail\s+delivery\s+(failed|subsystem)", re.IGNORECASE),
    re.compile(r"returning\s+message\s+to\s+sender", re.IGNORECASE),
    re.compile(r"delivery\s+status\s+notification", re.IGNORECASE),
    re.compile(r"delivery\s+(has\s+)?failed|failed\s+delivery", re.IGNORECASE),
    re.compile(r"undeliverable|undelivered\s+mail", re.IGNORECASE),
    re.compile(r"could\s+not\s+be\s+delivered", re.IGNORECASE),
    re.compile(r"address\(es\)\s+failed", re.IGNORECASE),
    re.compile(r"failure\s+notice", re.IGNORECASE),
    re.compile(r"action:\s*failed", re.IGNORECASE),
    re.compile(r"final-recipient:", re.IGNORECASE),
    re.compile(r"permanent\s+error", re.IGNORECASE),
]
# DSN senders: a message from these is a bounce regardless of wording.
_BOUNCE_SENDER_RE = re.compile(
    r"^(mailer-daemon|postmaster|mdaemon|bounces?)\b", re.IGNORECASE
)
# Automated but not-a-bounce senders (no-reply@, notifications@, ...): their
# mail is never a human reply, and bodies like "click here to unsubscribe"
# must not reach the negative keyword pass either.
_AUTOMATED_SENDER_RE = re.compile(
    r"^(no[-_]?reply|do[-_\s]?not[-_\s]?reply|donotreply|"
    r"auto[-_]?(generated|reply)|notifications?|alerts?|news(letter)?)\b",
    re.IGNORECASE,
)

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
    # Polite declines: no blunt "not interested", but zero interest either.
    # Without these a courteous "no" ("we are not exploring this type of
    # collaboration…") matches nothing and would be misread as a positive reply.
    re.compile(r"\bnot\s+(currently\s+|at\s+this\s+time\s+)?(exploring|seeking|pursuing|looking\s+(for|to)|in\s+the\s+market)", re.IGNORECASE),
    re.compile(r"\bnot\s+(a|the)\s+(good\s+|great\s+|right\s+)?fit\b", re.IGNORECASE),
    re.compile(r"\bnot\s+the\s+right\s+(time|person|moment)\b", re.IGNORECASE),
    re.compile(r"\b(no|don'?t|do\s+not)\s+(current\s+|immediate\s+)?need\b", re.IGNORECASE),
    re.compile(r"\b(have\s+)?sav(?:ed|e)\s+your\s+(contact|details|information)", re.IGNORECASE),
    re.compile(r"\b(keep|keeping)\s+(you|your\s+\w+)\s+(in\s+mind|on\s+file)\b", re.IGNORECASE),
    re.compile(r"\b(will|would|'ll)\s+(certainly\s+|definitely\s+|be\s+sure\s+to\s+)?(get|be)\s+in\s+touch\s+if\s+(the\s+)?(situation|needs?|circumstances?)", re.IGNORECASE),
    re.compile(r"\b(wish\s+you\s+(great\s+|much\s+|every\s+|continued\s+)?success|best\s+of\s+luck)\b", re.IGNORECASE),
    re.compile(r"\bpass\s+(on\s+this|for\s+now)\b", re.IGNORECASE),
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

# Display noise left behind by mail clients inside the plain-text part.
_IMAGE_PLACEHOLDER_RE = re.compile(r"\[image:\s*[^\]]*\]", re.IGNORECASE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def classify_sentiment(subject: str, reply_text: str, from_email: str = "") -> str:
    """Classify the sentiment of a prospect reply.

    Returns: 'positive' | 'auto_reply' | 'negative' | 'bounce'

    ``from_email`` (optional) lets the delivery-failure and automated-sender
    checks run before any keyword: a bounce is never a human sentiment, and a
    no-reply sender's notification text must not inherit keyword matches.
    """
    combined = f"{subject}\n{reply_text}"

    # 1. Bounce / DSN check — before every sentiment keyword. A delivery
    #    failure beats even rejection wording quoted inside the DSN body.
    sender = (from_email or "").strip().lower()
    local_part = sender.split("@", 1)[0]
    if _BOUNCE_SENDER_RE.match(local_part):
        return "bounce"
    for pat in _BOUNCE_PATTERNS:
        if pat.search(combined):
            return "bounce"

    # 2. Out of Office / Auto-reply check
    for pat in _OOO_PATTERNS:
        if pat.search(combined):
            return "auto_reply"

    # 3. Automated no-reply senders: their notifications are never human
    #    sentiment, so they must not fall through to the positive default.
    if _AUTOMATED_SENDER_RE.match(local_part):
        return "auto_reply"

    # 4. Negative / Unsubscribe check
    for pat in _NEGATIVE_PATTERNS:
        if pat.search(reply_text) or pat.search(subject):
            return "negative"

    # 5. Positive / Warm Lead check
    for pat in _POSITIVE_PATTERNS:
        if pat.search(reply_text):
            return "positive"

    # No keyword matched: the reply is a real human response but lacks clear
    # positive or negative signals. Classify as neutral rather than assuming
    # interest — false positives (marking a lukewarm "thanks" as a hot lead)
    # are worse than leaving a genuinely interested prospect unflagged.
    return "neutral"


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
        return _tidy_reply_text(cleaned)
    return _tidy_reply_text(full_body[:500])


def _tidy_reply_text(text: str) -> str:
    """Remove display noise that mail clients leave inside plain-text replies.

    Inline images arrive as ``[image: image]`` markers and clients pad the body
    with runs of blank lines and trailing spaces, all of which made the reply
    feed harder to read than the message itself.
    """
    if not text:
        return ""
    text = _IMAGE_PLACEHOLDER_RE.sub("", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip()


# How a reply's status came to be. Ingestion promotes a positive reply to
# ``lead`` automatically; the API's status endpoints record a human choice.
# The distinction lets a classifier correction undo its own promotion without
# overwriting a status a human deliberately set.
STATUS_SOURCE_AUTO = "auto"
STATUS_SOURCE_MANUAL = "manual"


def _demote_auto_lead(record: dict) -> bool:
    """Undo an automatic ``lead`` promotion after a sentiment correction.

    Ingestion sets ``status='lead'`` purely from a positive sentiment. When a
    later classifier run reads the same body differently, leaving the status
    behind keeps the green "Hot Lead Flagged" state, the "Marked Lead" filter
    and the card highlight pointing at a reply the classifier no longer
    considers positive — a false positive that never expires.

    Only the classifier's own promotion is undone: a status a human chose
    (``status_source == 'manual'``) is never touched. Records written before
    ``status_source`` existed are treated as automatic, because at ingestion
    time ``lead`` could only come from a positive sentiment — a human would
    have had to clear it first to set it by hand.

    Returns True when the status was changed.
    """
    if record.get("status") != "lead":
        return False
    if record.get("status_source") == STATUS_SOURCE_MANUAL:
        return False
    record["status"] = "unread"
    record["status_source"] = STATUS_SOURCE_AUTO
    return True


def _repairable_record_ids(records: list[dict]) -> set[str]:
    """Message-IDs of stored replies whose body could not be extracted.

    Replies ingested before the HTML-only body fallback existed were stored
    with an empty ``full_body`` (the feed renders them as "(Empty reply)").
    Their messages are already in the processed set, so a re-scan would skip
    them forever without this repair hook.
    """
    return {
        r.get("message_id") for r in records
        if r.get("message_id") and not (r.get("full_body") or "").strip()
    }


def collect_body_repairs(
    messages: list[email.message.EmailMessage],
    empty_ids: set[str],
) -> dict[str, dict]:
    """Re-extract bodies for processed messages stored with an empty body.

    ``messages`` are the already-parsed messages of the current scan window,
    so repairing costs no extra IMAP traffic. Only records whose new
    extraction is non-empty are returned; everything else stays untouched.
    """
    repairs: dict[str, dict] = {}
    for msg in messages:
        msg_id = (msg.get("Message-ID") or "").strip()
        if not msg_id or msg_id not in empty_ids or msg_id in repairs:
            continue
        reply_only = extract_clean_reply(extract_email_body(msg))
        if not reply_only.strip():
            continue
        _, repair_from = email.utils.parseaddr(msg.get("From", ""))
        sentiment = classify_sentiment(msg.get("Subject", ""), reply_only, repair_from)
        repairs[msg_id] = {
            "full_body": reply_only,
            "body_preview": reply_only[:200] + ("..." if len(reply_only) > 200 else ""),
            "sentiment": sentiment,
        }
    return repairs


async def apply_body_repairs(repairs: dict[str, dict]) -> int:
    """Persist re-extracted bodies onto their stored records (single write).

    Sentiment is re-classified from the recovered text; a repaired positive
    reply still in the default ``unread`` status becomes a ``lead``, mirroring
    ingestion. A correction *away* from positive undoes an automatic lead
    promotion (see ``_demote_auto_lead``); statuses a human has set are never
    overwritten. A recovered body that classifies as a bounce means the record
    was a DSN all along — it is removed from the feed instead of updated
    (delivery failures are never replies).
    """
    if not repairs:
        return 0
    async with _log_lock:
        records = load_log()
        changed = 0
        drop_ids: set[str] = set()
        for r in records:
            fix = repairs.get(r.get("message_id"))
            if not fix:
                continue
            if fix["sentiment"] == "bounce" and r.get("message_id"):
                drop_ids.add(r["message_id"])
                changed += 1
                continue
            r["full_body"] = fix["full_body"]
            r["body_preview"] = fix["body_preview"]
            r["sentiment"] = fix["sentiment"]
            if fix["sentiment"] == "positive":
                if r.get("status") == "unread":
                    r["status"] = "lead"
                    r["status_source"] = STATUS_SOURCE_AUTO
            else:
                _demote_auto_lead(r)
            changed += 1
        if changed:
            if drop_ids:
                records = [r for r in records if r.get("message_id") not in drop_ids]
            save_log(records)
    return changed


async def reclassify_stored_sentiments() -> int:
    """Re-run the classifier over stored reply bodies (single write).

    Records whose Message-ID is already in the processed set are skipped by
    every later scan, so a classifier improvement (e.g. a new polite-decline
    pattern) would never reach them and their badge would stay wrong forever.
    Sentiment is derived data and is safe to overwrite; statuses a human may
    have set are never touched, except for the same ``unread`` -> ``lead``
    promotion ingestion applies to a freshly positive reply, and the inverse
    demotion of a ``lead`` the classifier itself promoted when the correction
    reads the reply as no longer positive (see ``_demote_auto_lead``). A body
    the current classifier reads as a bounce is removed outright: DSNs are not
    replies (older versions stored them here, misflagged as hot leads), and
    they belong to the bounce pipeline instead.
    """
    async with _log_lock:
        records = load_log()
        changed = 0
        drop_ids: set[str] = set()
        for r in records:
            body = (r.get("full_body") or "").strip()
            if not body:
                continue  # empty bodies are the repair pass's job
            sentiment = classify_sentiment(
                r.get("subject", ""), body, r.get("from_email", "")
            )
            if sentiment == r.get("sentiment"):
                continue
            if sentiment == "bounce":
                # A delivery-failure notice is not a reply. Older versions
                # stored DSNs here (and the positive default even hot-flagged
                # them); they belong to the bounce pipeline, so remove them
                # from the feed. Records without a Message-ID cannot be keyed
                # for removal — re-tag instead, the read filter hides them.
                if r.get("message_id"):
                    drop_ids.add(r["message_id"])
                else:
                    r["sentiment"] = sentiment
                changed += 1
                continue
            r["sentiment"] = sentiment
            if sentiment == "positive":
                if r.get("status") == "unread":
                    r["status"] = "lead"
                    r["status_source"] = STATUS_SOURCE_AUTO
            else:
                # The old unknown-means-positive default hot-flagged replies
                # like a lone "hi". Correcting the badge is not enough: the
                # auto-promoted lead status has to go with it, or the card
                # keeps its "Hot Lead Flagged" state and lead-filter entry.
                _demote_auto_lead(r)
            changed += 1
        if changed:
            if drop_ids:
                records = [r for r in records if r.get("message_id") not in drop_ids]
            save_log(records)
    return changed


async def _ingest_inbox_dsn(
    listmonk: ListMonkClient,
    msg: email.message.EmailMessage,
    subject: str,
    campaigns: list,
    campaigns_ok: bool,
) -> bool:
    """Route a delivery-failure DSN caught by the reply scan to the bounce pipeline.

    The primary inbox receives bounce notices for campaigns sent from this
    account. They are never replies: they are classified and recorded through
    the same path as bounce-mailbox DSNs, so the Bounces page lists them and
    Analytics counts them. Returns True when the DSN reached a terminal
    outcome (record the idempotency key); False means transient — the next
    scan retries it.

    Lock order: ``_reply_scan_lock`` (held by the caller) → ``_INGEST_LOCK``.
    The bounce scan only ever takes ``_INGEST_LOCK``, so no cycle is possible.
    Serialising here means a mailbox both scanners watch can never run the
    check → create_bounce → mark sequence for the same DSN twice: the second
    scanner re-reads the processed set after the first one finished.
    """
    async with _INGEST_LOCK:
        state_id = _message_state_id(msg)
        if state_id and state_id in set(load_processed_bounce_ids()):
            # The bounce ingester already recorded this DSN (both scanners can
            # watch the same mailbox) — creating it again would duplicate it.
            return True
        try:
            outcome, detail = await ingest_parsed_dsn(
                listmonk, msg, subject, _extract_body(msg),
                recent_campaigns=campaigns, campaigns_ok=campaigns_ok,
                ingested_by="reply_tracker",
            )
        except Exception as e:
            logger.warning(f"[ReplyTracker] Inbox DSN ingestion failed (will retry): {e}")
            return False
        if outcome == "retry" or outcome == "retry_no_campaign":
            logger.warning(f"[ReplyTracker] Inbox DSN deferred ({detail}); the next scan retries it")
            return False
        if outcome == "ingested":
            logger.info(f"[ReplyTracker] Recorded inbox DSN as a {detail} bounce")
        if state_id:
            # Share the bounce ingester's processed set, so a mailbox watched by
            # both scanners yields exactly one bounce record per DSN.
            mark_processed_ids([state_id])
        return True


def _connect_imap() -> imaplib.IMAP4:
    # A socket timeout is mandatory: without it a hung mail server blocks the
    # worker thread — and holds _reply_scan_lock — forever.
    if settings.imap_use_ssl:
        client = imaplib.IMAP4_SSL(
            settings.imap_host, settings.imap_port,
            timeout=settings.imap_timeout,
        )
    else:
        client = imaplib.IMAP4(
            settings.imap_host, settings.imap_port,
            timeout=settings.imap_timeout,
        )
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
            "bounce": 0,
            "reclassified": 0,
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
            "neutral": 0,
            "auto_reply": 0,
            "bounce": 0,
            "reclassified": 0,
        }

    processed_ids = load_processed_message_ids()
    new_records = []
    new_msg_ids: list[str] = []
    stats = {
        "scanned": 0,
        "new_replies": 0,
        "positive": 0,
        "negative": 0,
        "neutral": 0,
        "auto_reply": 0,
        "bounce": 0,
        "repaired": 0,
        "reclassified": 0,
    }

    # Fetch the campaign history once per scan, walked back far enough to
    # cover every reply in the scan window. A per-message fetch (or a single
    # page) both mis-attributes old replies and hammers the API. Complete
    # starts False so a failed fetch also gates DSN attribution (a bounce
    # with no campaign history is retried next scan, never discarded).
    campaigns_complete = False
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

            # imap_date, not strftime %b: IMAP SEARCH SINCE requires English
            # month abbreviations, which %b only guarantees under the C
            # locale — the same reason the other scanners use the helper.
            since = imap_date(datetime.now(timezone.utc) - timedelta(days=days_back))
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

    # Repair replies that were stored with an empty body (e.g. ingested before
    # the HTML-only fallback existed). Their messages are in the processed set,
    # so the loop below would skip them forever; the full messages are already
    # in memory, so re-extraction costs nothing extra.
    try:
        repairs = collect_body_repairs(messages, _repairable_record_ids(load_log()))
        if repairs:
            stats["repaired"] = await apply_body_repairs(repairs)
            if stats["repaired"]:
                logger.info(
                    f"[ReplyTracker] Repaired {stats['repaired']} reply record(s) "
                    "with re-extracted bodies"
                )
    except Exception as e:
        logger.warning(f"[ReplyTracker] Body repair pass failed (non-fatal): {e}")

    # Re-tag stored records the (evolved) classifier now reads differently:
    # their Message-IDs are in the processed set, so the ingest loop below
    # would skip them forever and their badge would stay stale.
    try:
        stats["reclassified"] = await reclassify_stored_sentiments()
        if stats["reclassified"]:
            logger.info(
                f"[ReplyTracker] Re-classified {stats['reclassified']} stored "
                "reply record(s) with the current sentiment rules"
            )
    except Exception as e:
        logger.warning(f"[ReplyTracker] Sentiment re-classification failed (non-fatal): {e}")

    my_email = (settings.imap_user or "").strip().lower()

    for msg in messages:
        stats["scanned"] += 1
        msg_id = (msg.get("Message-ID") or "").strip()
        # Messages without a Message-ID still need an idempotency key, or the
        # 15-minute scan re-records them on every run across the whole 14-day
        # window. Fall back to the From|Date|Subject fingerprint the bounce
        # ingester uses.
        state_id = msg_id or _message_state_id(msg)
        if state_id and state_id in processed_ids:
            continue

        from_header = msg.get("From", "")
        realname, from_email = email.utils.parseaddr(from_header)
        from_email = from_email.strip().lower()

        # Skip our own outgoing emails or empty addresses
        if not from_email or from_email == my_email:
            if state_id:
                new_msg_ids.append(state_id)
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

        sentiment = classify_sentiment(subject, reply_only, from_email)

        # A delivery-failure DSN is never a reply: route it to the bounce
        # pipeline (Bounces page, Analytics bounce counts) and keep it out
        # of the feed entirely.
        if sentiment == "bounce":
            stats["bounce"] += 1
            handled = await _ingest_inbox_dsn(
                listmonk, msg, subject, campaigns, campaigns_complete,
            )
            if handled and state_id:
                new_msg_ids.append(state_id)
            continue

        # Match campaign (history was fetched once for the whole scan).
        # Automated responses (OOO, ticket confirmations, generic auto-replies)
        # are never genuine prospect engagement — skip attribution entirely so
        # they don't receive a campaign tag or inflate per-campaign charts.
        if sentiment == "auto_reply":
            camp_id, camp_name = None, None
        else:
            camp_id, camp_name = await match_campaign_for_reply(
                listmonk, subject, from_email, reply_date, campaigns=campaigns,
            )

        # Note: Replies without campaign attribution are still stored with
        # campaign_id=null. While ideally we'd skip non-campaign emails, doing
        # so risks losing valid replies when matching fails due to incomplete
        # data (deleted campaigns, changed subjects, etc.). The neutral sentiment
        # default ensures these aren't falsely flagged as hot leads, and
        # campaign_name stays null so the feed labels them "Unattributed"
        # instead of inventing a campaign they never belonged to.
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
            # No fallback label: a reply that matched no campaign must not
            # display as if it belonged to one.
            "campaign_name": camp_name,
            "sentiment": sentiment,
            "status": "lead" if sentiment == "positive" else "unread",
            # Provenance for the auto-promotion above, so a later sentiment
            # correction can undo it without touching a human's status choice.
            "status_source": STATUS_SOURCE_AUTO,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        new_records.append(record)
        stats["new_replies"] += 1
        stats[sentiment] = stats.get(sentiment, 0) + 1

        # Persist the idempotency key (Message-ID or fingerprint), not just
        # the raw Message-ID, so header-less replies are deduped too.
        if state_id:
            new_msg_ids.append(state_id)

    if new_records:
        await append_log(new_records)
        logger.info(f"IMAP Reply Tracker: Recorded {len(new_records)} new prospect replies")

    # One read+write for the whole scan instead of one per message.
    if new_msg_ids:
        mark_processed_message_ids(new_msg_ids)

    return stats
