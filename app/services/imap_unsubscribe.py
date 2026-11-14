"""
IMAP Unsubscribe Monitor: Scans an IMAP inbox for reply emails containing
unsubscribe keywords. When found, automatically unsubscribes the sender
from all ListMonk lists and (if enabled in settings) blocklists them.

Every message in the lookback window is considered regardless of IMAP
\\Seen — the inbox is shared with a human — and unsubscribe_examined.json
(Message-ID / fingerprint) keeps a message from being evaluated twice.

Storage: unsubscribe_log.json (same pattern as schedule.json)
"""

import asyncio
import imaplib
import email
import email.policy
import email.utils
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import settings
from app.services import reply_i18n as _i18n
from app.services.listmonk_client import ListMonkClient
from app.services.unsubscribe_log import (
    load_log, save_log, append_log, load_settings,
    load_processed_emails, mark_processed,
    load_examined_ids, mark_examined_ids, examined_state_exists, add_link_seen,
    _log_lock,
)
from app.services.imap_helpers import (
    safe_email_for_query, imap_date, extract_email_body,
    fetch_state_ids, message_state_id, campaign_has_sent, campaign_send_dt,
    strip_campaign_echo, campaign_echo_index_for,
    automated_header_kind,
)

logger = logging.getLogger("imap_unsubscribe")

# Safety cap when walking campaign history backwards for re-attribution.
MAX_CAMPAIGN_PAGES = 20  # 2000 campaigns
CAMPAIGN_PAGE_SIZE = 100

# Always scan at least this far back for unseen unsubscribe replies. An
# unsubscribe for a campaign sent last month can arrive after this month's
# campaign has already moved the "campaign month" window forward.
MIN_LOOKBACK_DAYS = 30

UNSUBSCRIBE_KEYWORDS = [
    "remove me",
    "unsubscribe me",
    "exclude me",
    # B2B prospects answer for their company: "please remove us from your
    # list". Same phrases, plural subject — an opt-out that was never acted on
    # while the reply feed already tagged it "not interested".
    "remove us",
    "unsubscribe us",
    "exclude us",
]

# Instructions that are not one of the literal phrases above but ask just as
# plainly to stop the mail. Every one needs an object ("take us off your
# list") or an explicit "no more mail"; a bare "unsubscribe" is only an
# opt-out when it is the whole reply. "Don't stop emailing me" is a
# compliment, hence the lookbehinds on the stop-verbs.
_OPT_OUT_PATTERNS = [
    r"\b(?:please|kindly|pls)\s+unsubscribe\b",
    r"\b(?:i|we)(?:\s+(?:would\s+like|want|wish|need)|['’]d\s+like)\s+to\s+unsubscribe\b",
    r"\btake\s+(?:me|us)\s+off\s+(?:of\s+)?(?:your|the|this|that|any|all)\s+(?:\w+\s+){0,2}"
    r"(?:lists?|mailing|distribution|database|contacts?|e-?mails?|campaigns?)\b",
    r"\b(?:remove|delete)\s+(?:my|our)\s+(?:e-?mail(?:\s+address)?|address|details|"
    r"contact(?:\s+(?:details|information))?|information|data|name)\b"
    r"(?!\s+from\s+(?:this|the)\s+(?:thread|chain|conversation|cc|loop))",
    r"(?<!n't )(?<!n’t )(?<!not )(?<!never )\bstop\s+(?:e-?mailing|sending|contacting|messaging|writing)\b",
    r"\b(?:do\s*n[o']?t|do\s+not|never)\s+(?:e-?mail|contact|message|write\s+to)\s+(?:me|us)\b",
    r"\bno\s+(?:more|further)\s+(?:e-?mails?|mails?|messages?|contact|correspondence|communications?)\b"
    r"(?!\s+(?:is\s+|are\s+)?(?:needed|necessary|required))",
    r"\bopt\s+(?:me|us)\s+out\b|\bopt[\s-]*out\s+of\s+(?:your|the|this|these|all)\b",
    # The whole answer is the instruction: "Unsubscribe", "STOP", "Remove".
    r"\A\s*(?:please\s+)?(?:unsubscribe|stop|remove|opt[\s-]*out)\s*[.!]*\s*(?:\n|\Z)",
]

KEYWORD_PATTERN = re.compile(
    "|".join(
        # Word-bounded: as bare substrings "remove me" matched "remove
        # mention", "exclude me" matched "exclude media" and "remove us"
        # matched "remove users" -- each one unsubscribing (and possibly
        # blocklisting) a prospect who never asked for it.
        [rf"\b{re.escape(kw)}\b" for kw in UNSUBSCRIBE_KEYWORDS]
        + [f"(?:{p})" for p in _OPT_OUT_PATTERNS]
        + [f"(?:{p.pattern})" for p in _i18n.OPT_OUT_PATTERNS]
    ),
    re.IGNORECASE,
)

# The "wrote" verb of reply attribution lines in the locales cold outreach
# meets most. Only English "wrote:" used to be recognised, so a French
# ("a écrit :"), German ("schrieb"), or Spanish ("escribió") reply kept our
# whole quoted campaign as if the prospect had written it.
_WROTE_VERB = (
    r"(?:wrote|a\s+[ée]crit|schrieb|escribi[óo]|ha\s+scritto|schreef|"
    r"escreveu|skrev|napisa[łl]|kirjoitti)"
)
_ATTRIBUTION_OPENER = r"(?:On|Le|Am|El|Il|Op|Em|Den|P[åa]|W\s+dniu)"

# "On <date>,\n<person> wrote:" quote headers wrapped over up to a few lines,
# which a line-by-line scan cannot see. The inner part rejects blank lines so
# the match cannot jump across a paragraph break into unrelated text. The verb
# may be followed by the sender ("Am … schrieb Sam <sam@x.com>:").
_MULTILINE_QUOTE_RE = re.compile(
    rf"^{_ATTRIBUTION_OPENER}\s+[^\n]*(?:\n(?![ \t]*\n)[^\n]*){{0,3}}?"
    rf"\b{_WROTE_VERB}\b[^\n]*:\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Single-line attribution: ends with ":" and carries a "wrote" verb, plus an
# opener word, an address or a date digit — so "My boss wrote:" stays text,
# while "Sam <sam@x.com> schrieb am Mo., 3. Nov.:" (Gmail mobile) is a quote.
_ATTRIBUTION_LINE_RE = re.compile(rf"\b{_WROTE_VERB}\b.*:\s*$", re.IGNORECASE)
_ATTRIBUTION_HINT_RE = re.compile(rf"^{_ATTRIBUTION_OPENER}\s|@|\d", re.IGNORECASE)

# Outlook-style quoted header block: a "From:" label (any locale, possibly
# bold "*From:*" in plain-text renderings) followed within a few lines by
# another header label. Outlook writes "From: Sam Sender" without an address
# and "Sent: Monday, November 3…" without a leading digit, which the old
# "From: …@…" / "Sent: <digit>" rules both missed — the quoted campaign then
# counted as the prospect's own words.
_HEADER_FROM_RE = re.compile(
    r"^\*?(From|Von|De|Da|Van|Fr[åa]n|Fra|Od|L[äa]hett[äa]j[äa]|Felad[óo])\s*:\*?\s*\S",
    re.IGNORECASE,
)
_HEADER_FIELD_RE = re.compile(
    r"^\*?(Sent|Date|To|Subject|Cc|Gesendet|Datum|An|Betreff|Envoy[ée]|[ÀA]|Objet|"
    r"Enviado|Fecha|Para|Asunto|Inviato|Data|Oggetto|Verzonden|Aan|Onderwerp|"
    r"Skickat|Till|[ÄA]mne|Sendt|Til|Emne|L[äa]hetetty|Vastaanottaja|Aihe)\s*:",
    re.IGNORECASE,
)

# Mobile signatures always close the prospect's own text; anything below them
# is the quoted thread.
_MOBILE_SIGNATURE_RE = re.compile(
    r"^(Sent from my \w+|Get Outlook for (iOS|Android)|Sent from Outlook|"
    r"Von meinem \w+ gesendet|Envoy[ée] de mon \w+|Enviado desde mi \w+|"
    r"Inviato da \w+|Verzonden met \w+|Skickat fr[åa]n min \w+)\b",
    re.IGNORECASE,
)


def _is_header_block_start(lines: list[str], i: int) -> bool:
    """True when ``lines[i]`` opens a quoted "From:/Sent:/To:" header block."""
    if not _HEADER_FROM_RE.match(lines[i].strip()):
        return False
    seen = 0
    for nxt in lines[i + 1:]:
        stripped = nxt.strip()
        if not stripped:
            continue
        if _HEADER_FIELD_RE.match(stripped):
            return True
        seen += 1
        if seen >= 3:
            break
    return False


def _extract_body(msg: email.message.EmailMessage) -> str:
    return extract_email_body(msg)


def _extract_reply_only(full_body: str) -> str:
    """
    Extract ONLY the user's reply text, stripping all quoted/forwarded
    content from the email body.

    This prevents false-positive keyword matches from our own email
    template text (e.g., footer saying "Reply with 'Remove me'").

    Handles common quote patterns:
    - Lines starting with ">" (standard quoting)
    - "On <date> <someone> wrote:" markers, including headers wrapped over
      several lines (Gmail)
    - "From: <address>" forwarding headers
    - "-----Original Message-----" (Outlook)
    - "Sent: " / "To: " / "Subject: " header blocks in quoted replies
    - Outlook "From: Name" + "Sent: Monday, …" blocks without an address,
      in any common locale ("Von:/Gesendet:", "De:/Envoyé:", …)
    - Localised attribution lines ("a écrit :", "schrieb", "escribió", …)
    - Mobile signatures ("Sent from my iPhone") and the "-- " delimiter

    Callers that know the campaign the reply answers should also pass the
    result through ``imap_helpers.strip_campaign_echo`` — the backstop for
    quote headers no pattern here recognises.
    """
    lines = full_body.splitlines()
    reply_lines = []

    # Multi-line "On <date>,\n  <person> wrote:" headers cannot be detected
    # line-by-line, so cut the body at the first one before the scan below.
    multiline_quote = _MULTILINE_QUOTE_RE.search(full_body)
    if multiline_quote:
        lines = full_body[:multiline_quote.start()].splitlines()

    # Patterns that indicate the start of quoted/forwarded content
    quote_start_patterns = [
        # "On Mon, Jan 1, 2026 at 12:00 PM John Doe <john@example.com> wrote:"
        re.compile(r'^On\s+.+wrote:\s*$', re.IGNORECASE),
        # "-----Original Message-----" (and its localised / forwarded forms)
        re.compile(
            r"^-{2,}\s*(Original Message|Urspr[üu]ngliche Nachricht|Message d'origine|"
            r"Mensaje original|Messaggio originale|Oorspronkelijk bericht|"
            r"Ursprungligt meddelande|Opprinnelig melding|Oprindelig meddelelse|"
            r"Mensagem original|Forwarded message|Weitergeleitete Nachricht|"
            r"Message transf[ée]r[ée])\s*-{2,}",
            re.IGNORECASE,
        ),
        re.compile(r'^(Begin forwarded message|Anfang der weitergeleiteten Nachricht)\s*:', re.IGNORECASE),
        # RFC 3676 signature delimiter ("-- "): company footers below it carry
        # words like "unsubscribe" that are not the prospect's request.
        re.compile(r'^--$'),
        # "From: Name <email>" or "From: email" at start of line (forwarded header)
        re.compile(r'^From:\s+.+@.+', re.IGNORECASE),
        # "Sent: 3/7/26 12:17 AM" (Outlook-style quoted header)
        re.compile(r'^Sent:\s+\d', re.IGNORECASE),
        # "________" or "========" separator lines (common in some clients)
        re.compile(r'^[_=]{5,}\s*$'),
        # Gmail-style: "> " quoted lines (3+ consecutive = definitely quoted block)
        # We handle ">" lines individually below
    ]

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Skip empty lines at the very beginning
        if not reply_lines and not stripped:
            continue

        # Check if this line starts a quoted section
        is_quote_start = False
        for pattern in quote_start_patterns:
            if pattern.match(stripped):
                is_quote_start = True
                break

        if not is_quote_start:
            is_quote_start = (
                _is_header_block_start(lines, i)
                or bool(_MOBILE_SIGNATURE_RE.match(stripped))
                or (bool(_ATTRIBUTION_LINE_RE.search(stripped))
                    and bool(_ATTRIBUTION_HINT_RE.search(stripped)))
            )

        if is_quote_start:
            # Everything from here is quoted content — stop collecting
            break

        # Lines starting with ">" are quoted text — skip them
        if stripped.startswith('>'):
            # If we haven't collected any reply yet, keep looking
            # If we already have reply text and hit ">", the reply is above
            if reply_lines:
                break
            continue

        reply_lines.append(line)

    reply_text = '\n'.join(reply_lines).strip()

    # If we couldn't extract a reply (e.g., entire body is quoted),
    # return empty string so no false match occurs
    return reply_text


def _extract_sender_email(msg: email.message.EmailMessage) -> Optional[str]:
    """Extract the sender's email address from the From header.

    The header is parsed as an address (``parseaddr``), never searched with a
    regex: an address in the *display name* ("victim@corp.com"
    <attacker@evil.com>, or "Smith (john@old-corp.com)" <jsmith@corp.com>)
    used to win, so a reply unsubscribed — and possibly blocklisted — someone
    other than its sender.
    """
    _, addr = email.utils.parseaddr(str(msg.get("From") or ""))
    addr = addr.strip().lower()
    if "@" not in addr or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", addr):
        return None
    return addr


def _clean_subject(subject: str) -> str:
    """Strip Re:/Fwd:/FW: prefixes and extra whitespace."""
    cleaned = re.sub(r'^(Re|Fwd|FW|Fw)\s*:\s*', '', subject, flags=re.IGNORECASE).strip()
    # Recursively strip if multiple prefixes
    if re.match(r'^(Re|Fwd|FW|Fw)\s*:', cleaned, re.IGNORECASE):
        return _clean_subject(cleaned)
    return cleaned


def _parse_campaign_dt(created: str) -> Optional[datetime]:
    """Parse a ListMonk ``created_at`` string into an aware UTC datetime."""
    if not created:
        return None
    try:
        camp_date = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if camp_date.tzinfo is None:
            camp_date = camp_date.replace(tzinfo=timezone.utc)
        return camp_date
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(created[:10]).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None


def _match_campaign(
    campaigns: list[dict],
    email_date: Optional[datetime] = None,
    sub_list_ids: Optional[set] = None,
) -> Optional[dict]:
    """
    Match a reply email to the most recent ListMonk campaign created on or
    before the email date whose target lists intersect the subscriber's lists.

    If `sub_list_ids` is provided, only campaigns whose `lists` include at
    least one of those IDs are considered — this prevents attributing a reply
    to a campaign the subscriber never received. If a campaign has no `lists`
    field populated (API sometimes omits it), fall back to date-only matching
    for that campaign, mirroring link_unsubscribe._pick_campaign_for_list_ids.

    Returns {campaign_id, campaign_name, campaign_subject, matched_list_id}
    or None if nothing qualifies.
    """
    if not email_date:
        email_date = datetime.now(timezone.utc)
    if not campaigns:
        return None

    best_match = None
    best_date = None

    for camp in campaigns:
        # A draft / scheduled campaign never reached the sender, so it can be
        # neither what they replied to nor what they unsubscribed from.
        if not campaign_has_sent(camp):
            continue
        camp_date = campaign_send_dt(camp)
        if camp_date is None:
            continue

        if camp_date > email_date:
            continue

        camp_list_ids = [l.get("id") for l in (camp.get("lists") or [])]
        matched_list_id = None
        if sub_list_ids and camp_list_ids:
            for lid in camp_list_ids:
                if lid in sub_list_ids:
                    matched_list_id = lid
                    break
            if matched_list_id is None:
                continue

        if best_date is None or camp_date > best_date:
            best_date = camp_date
            best_match = {
                "campaign_id": camp.get("id"),
                "campaign_name": camp.get("name", ""),
                "campaign_subject": camp.get("subject", ""),
                "matched_list_id": matched_list_id,
            }

    return best_match


def connect_imap() -> Optional[imaplib.IMAP4_SSL | imaplib.IMAP4]:
    """Connect to the IMAP server. Returns None on failure."""
    if not settings.imap_configured:
        logger.warning("IMAP not configured, skipping scan")
        return None
    try:
        if settings.imap_use_ssl:
            conn = imaplib.IMAP4_SSL(
                settings.imap_host, settings.imap_port,
                timeout=settings.imap_timeout,
            )
        else:
            conn = imaplib.IMAP4(
                settings.imap_host, settings.imap_port,
                timeout=settings.imap_timeout,
            )
        conn.login(settings.imap_user, settings.imap_pass)
        return conn
    except Exception as e:
        logger.error(f"IMAP connection failed: {e}")
        return None


async def check_imap_status() -> dict:
    """Check IMAP connection status without scanning (off the event loop)."""
    if not settings.imap_configured:
        return {"configured": False, "connected": False, "error": "IMAP not configured in .env"}
    try:
        conn = await asyncio.to_thread(connect_imap)
        if conn:
            await asyncio.to_thread(conn.logout)
            return {"configured": True, "connected": True, "error": None}
        return {"configured": True, "connected": False, "error": "Connection failed"}
    except Exception as e:
        return {"configured": True, "connected": False, "error": str(e)}


_scan_lock = asyncio.Lock()
_scan_running = False


def _has_active_subscription(subscriber: dict) -> bool:
    """True when the subscriber can still receive campaign mail.

    Enabled, and on at least one list whose subscription is not
    ``unsubscribed``. A list entry without a ``subscription_status`` (older
    ListMonk payloads) counts as active: when unsure, honour the request.
    """
    if subscriber.get("status") == "blocklisted":
        return False
    return any(
        (lst.get("subscription_status") or "") != "unsubscribed"
        for lst in subscriber.get("lists") or [] if isinstance(lst, dict)
    )


def _oldest_pending_record_dt(records: list[dict]) -> Optional[datetime]:
    """Oldest timestamp among email-source records still awaiting attribution."""
    oldest = None
    for r in records:
        if r.get("source") != "email" or r.get("reattributed_at"):
            continue
        if not r.get("lists_removed"):
            continue
        ts = r.get("timestamp", "")
        try:
            rec_date = datetime.fromisoformat(ts) if ts else datetime.now(timezone.utc)
        except (ValueError, TypeError):
            continue
        if rec_date.tzinfo is None:
            rec_date = rec_date.replace(tzinfo=timezone.utc)
        else:
            rec_date = rec_date.astimezone(timezone.utc)
        if oldest is None or rec_date < oldest:
            oldest = rec_date
    return oldest


async def _fetch_campaign_history(client: ListMonkClient, cutoff: Optional[datetime]) -> tuple[list[dict], bool, bool]:
    """Fetch campaigns newest-first, walking back until ``cutoff`` is covered.

    Returns ``(campaigns, complete_all, covers_cutoff)``:

    * ``complete_all`` — every campaign in ListMonk was fetched. Only then is
      it safe to prune records whose campaign no longer exists.
    * ``covers_cutoff`` — the fetched window provably contains every campaign
      that could match a record at or after ``cutoff``. Re-attribution against
      a truncated newest-first list would silently reassign old records to
      whichever recent campaign happens to share a list, so it must be False
      whenever we stopped early. When ``cutoff`` is None there is no pending
      re-attribution work, so a single page is both cheap and safe.
    """
    if cutoff is None:
        # Steady state: nothing left to re-attribute, so one page is enough
        # for matching new replies. complete_all still lets pruning run for
        # the common case of a campaign count that fits on one page.
        try:
            result = await client.get_campaigns(
                page=1, per_page=CAMPAIGN_PAGE_SIZE,
                order_by="created_at", order="DESC",
            )
        except Exception as e:
            logger.error(f"[IMAP] Failed to fetch campaigns: {e}")
            return [], False, False
        data = result.get("data") or {}
        results = data.get("results", [])
        complete = results and data.get("total", 0) <= len(results)
        return results, bool(complete), True

    campaigns: list[dict] = []
    page = 1
    while True:
        try:
            result = await client.get_campaigns(
                page=page, per_page=CAMPAIGN_PAGE_SIZE,
                order_by="created_at", order="DESC",
            )
        except Exception as e:
            logger.error(f"[IMAP] Failed to fetch campaigns: {e}")
            return campaigns, False, False

        data = result.get("data") or {}
        results = data.get("results", [])
        if not results:
            return campaigns, True, True  # reached the end of history
        campaigns.extend(results)

        oldest_seen = None
        for c in campaigns:
            dt = _parse_campaign_dt(c.get("created_at", ""))
            if dt is not None and (oldest_seen is None or dt < oldest_seen):
                oldest_seen = dt
        covers_cutoff = oldest_seen is not None and oldest_seen <= cutoff

        if page >= MAX_CAMPAIGN_PAGES:
            if not covers_cutoff:
                logger.warning(
                    f"[IMAP] Stopped campaign history at {MAX_CAMPAIGN_PAGES} pages "
                    "without covering the oldest pending record; skipping "
                    "re-attribution to avoid mis-assigning it"
                )
            return campaigns, False, covers_cutoff
        page += 1


def _reattribute_existing_records(
    records: list[dict], campaigns_list: list[dict], allow_removal: bool = True
) -> tuple[int, int]:
    """
    One-time backfill: re-run list-aware attribution on existing email-source
    records that were stored with the old date-only matcher.

    - If a record matches a different campaign, update it in place.
    - If a record matches no campaign it is kept as it is; with the complete
      campaign list (`allow_removal`) it is marked settled so it is not
      re-examined. Records are never removed: each is an opt-out's audit trail.
    - Skips records already marked with `reattributed_at`, records without
      `lists_removed`, and non-email records.

    Returns (changed_count, settled_count): records whose campaign changed,
    and records newly stamped ``reattributed_at`` (the caller saves when
    either is non-zero, so a settled record is not re-examined every scan).
    """
    changed = 0
    settled = 0
    for r in records:
        if r.get("source") != "email":
            continue
        if r.get("reattributed_at"):
            continue
        lists_removed = r.get("lists_removed") or []
        if not lists_removed:
            continue

        ts = r.get("timestamp", "")
        try:
            rec_date = datetime.fromisoformat(ts) if ts else datetime.now(timezone.utc)
            if rec_date.tzinfo is None:
                rec_date = rec_date.replace(tzinfo=timezone.utc)
            else:
                rec_date = rec_date.astimezone(timezone.utc)
        except (ValueError, TypeError):
            rec_date = datetime.now(timezone.utc)

        new_match = _match_campaign(
            campaigns_list, rec_date, set(lists_removed)
        )
        if new_match:
            new_cid = new_match["campaign_id"]
            if r.get("campaign_id") != new_cid:
                r["campaign_id"] = new_cid
                r["campaign_name"] = new_match["campaign_name"]
                r["matched_list_id"] = new_match.get("matched_list_id")
                changed += 1
            r["reattributed_at"] = datetime.now(timezone.utc).isoformat()
            settled += 1
        elif allow_removal:
            # Nothing matches even with the complete history: the record keeps
            # its stored attribution (it is still a real opt-out, and Undo
            # works from it) and is not looked at again. It used to be
            # deleted from the log here.
            r["reattributed_at"] = datetime.now(timezone.utc).isoformat()
            settled += 1

    return changed, settled


async def scan_and_unsubscribe(client: ListMonkClient) -> dict:
    """
    Scan IMAP inbox for unsubscribe requests and process them.
    Returns summary of actions taken.
    """
    global _scan_running
    if _scan_running:
        return {"scanned": 0, "matched": 0, "processed": 0, "errors": 0,
                "message": "Scan already in progress"}

    async with _scan_lock:
        _scan_running = True
        try:
            conn = await asyncio.to_thread(connect_imap)
        except BaseException:
            # Cancelled (client disconnect, shutdown) or failed while
            # connecting: the try/finally below does not cover this await, and
            # a flag left set answered "Scan already in progress" until restart.
            _scan_running = False
            raise
        if not conn:
            _scan_running = False
            return {"scanned": 0, "matched": 0, "processed": 0, "errors": 0,
                    "message": "IMAP not configured or connection failed"}

        processed = 0
        matched = 0
        errors = 0
        scanned = 0
        # Opt-out requests from addresses that are not ListMonk subscribers
        # (an alias, a colleague answering): nothing can be unenrolled, so
        # they are reported instead of vanishing into an info log line.
        unmatched = 0
        new_records = []
        # Senders handled earlier in this same scan (two requests in one run).
        handled_this_scan: set[str] = set()
        # Idempotency keys of messages that reached a final outcome (handled,
        # or not an actionable unsubscribe request) — see EXAMINED_FILE.
        examined_now: list[str] = []
        seen_after_persist: list = []
        # Per-scan cache of campaign/template copy for the echo backstop.
        echo_cache: dict = {}

        try:
            # Campaign history, newest first. One page is enough to match
            # *new* replies; older pages are only needed to re-attribute
            # records written before list-aware matching existed.
            try:
                campaigns_list, campaigns_complete, covers_cutoff = (
                    await _fetch_campaign_history(
                        client, _oldest_pending_record_dt(load_log())
                    )
                )
                logger.info(
                    f"[IMAP] Fetched {len(campaigns_list)} campaigns for matching "
                    f"(complete={campaigns_complete}, covers_cutoff={covers_cutoff})"
                )
            except Exception as e:
                logger.error(f"[IMAP] ERROR fetching campaigns: {e}")
                campaigns_list = []
                campaigns_complete = False
                covers_cutoff = False

            # A failed page-1 fetch is the exact shape ([], False, False) — a
            # genuine "no campaigns" instance returns ([], True, True).
            # Proceeding would match every keyword reply against an empty
            # campaign list, mark those messages \Seen as skips, and the UNSEEN
            # search would never look at them again: real unsubscribe requests
            # silently lost for good. Abort instead; the next hourly scan
            # retries with nothing consumed.
            if not campaigns_list and not campaigns_complete and not covers_cutoff:
                logger.error(
                    "[IMAP] Campaign history unavailable; aborting the scan so "
                    "no unsubscribe request is skipped-and-seen. Will retry."
                )
                return {"scanned": 0, "matched": 0, "processed": 0, "errors": 1,
                        "message": "Campaign history unavailable — scan aborted, will retry"}

            # Backfill + prune must be atomic with respect to other writers
            # (link scanner, reset/delete endpoints) so no records are lost.
            async with _log_lock:
                # Load log once and reuse across backfill and dedup.
                existing_log = load_log()

                if campaigns_list and covers_cutoff:
                    try:
                        changed, settled = _reattribute_existing_records(
                            existing_log, campaigns_list,
                            allow_removal=campaigns_complete,
                        )
                        if changed or settled:
                            save_log(existing_log)
                            logger.info(
                                f"[IMAP] Backfill: re-attributed {changed} of "
                                f"{settled} settled records"
                            )
                    except Exception as e:
                        logger.warning(f"[IMAP] Backfill reattribution failed (non-fatal): {e}")

                # Records of campaigns since deleted in ListMonk are KEPT:
                # they are an opt-out's audit trail and what "Undo All" works
                # from, and Settings → Automation lists them as "deleted
                # campaign" (GET /api/unsubscribes/orphans). The scan used to
                # prune them every hour, emptying that view.

                # Dedup sources (message ids from the log, emails from the
                # persistent processed set that survives log deletion/clearing).
                processed_msg_ids = {
                    r.get("message_id") for r in existing_log if r.get("message_id")
                }

            processed_emails_set = load_processed_emails()

            # Determine the latest campaign's creation date to filter emails
            latest_campaign_date = None
            for camp in campaigns_list:
                created = camp.get("created_at", "")
                if created:
                    try:
                        latest_campaign_date = datetime.fromisoformat(created[:10]).replace(tzinfo=timezone.utc)
                        break  # campaigns are sorted DESC, first one is latest
                    except (ValueError, TypeError):
                        continue

            await asyncio.to_thread(conn.select, "INBOX")

            # Every message in the window is considered, read or not: the
            # inbox is read by a human, and a "remove me" reply opened in
            # webmail before the hourly scan used to be invisible to an
            # UNSEEN search forever — a silently ignored opt-out (AGENTS.md
            # rule 13). The persisted examined set, not \Seen, decides what was
            # already handled. Never look back less than MIN_LOOKBACK_DAYS:
            # anchoring on the newest campaign *month* alone means a late reply
            # to an older campaign becomes invisible as soon as a newer
            # campaign starts.
            min_since = datetime.now(timezone.utc) - timedelta(days=MIN_LOOKBACK_DAYS)
            if latest_campaign_date:
                since_date = min(latest_campaign_date.replace(day=1), min_since)
                logger.info(
                    f"[IMAP] Searching emails SINCE {imap_date(since_date)} "
                    f"(campaign month, {MIN_LOOKBACK_DAYS}-day floor)"
                )
            else:
                # Fallback: no campaigns found at all
                since_date = min_since
                logger.warning(
                    f"[IMAP] No campaigns found, searching emails "
                    f"SINCE {imap_date(since_date)}"
                )

            since_str = imap_date(since_date)
            status, msg_ids = await asyncio.to_thread(
                conn.search, None, f'(SINCE {since_str})'
            )

            if status != "OK" or not msg_ids or not msg_ids[0]:
                return {"scanned": 0, "matched": 0, "processed": 0, "errors": 0,
                        "message": "No emails found in inbox for the campaign period"}

            ids = [m for m in msg_ids[0].split() if m]

            if not examined_state_exists():
                # One-time migration from the \Seen-based scanner: it fetched
                # (and so flagged \Seen) every message it ever looked at, and
                # "Undo All" relied on that to never re-process a reverted
                # request. Seeding the examined set with what is already \Seen
                # keeps that history exactly as the old version treated it;
                # from here on, read state no longer decides anything.
                s_status, seen_ids = await asyncio.to_thread(
                    conn.search, None, f'(SEEN SINCE {since_str})'
                )
                seen_list = (
                    [m for m in seen_ids[0].split() if m]
                    if s_status == "OK" and seen_ids and seen_ids[0] else []
                )
                seeded = (
                    await asyncio.to_thread(fetch_state_ids, conn, seen_list)
                    if seen_list else {}
                )
                await mark_examined_ids(seeded.values(), create=True)
                logger.info(
                    f"[IMAP] Seeded the examined set with {len(seeded)} "
                    "already-read message(s) (first scan of this version)"
                )

            # Cheap identity-header peek (batched, never sets \Seen) so
            # messages examined by an earlier scan cost no body download.
            state_ids = await asyncio.to_thread(fetch_state_ids, conn, ids)
            examined = set(load_examined_ids())
            pending = [m for m in ids if not (state_ids.get(m) and state_ids[m] in examined)]
            scanned = len(pending)
            logger.info(
                f"[IMAP] {len(ids)} emails in campaign period, "
                f"{scanned} not yet examined"
            )

            for msg_id in pending:
                try:
                    # BODY.PEEK[], not RFC822: fetching RFC822 sets \Seen as a
                    # side effect, so merely examining a message flipped the
                    # human's unread mail to read — and a message that then
                    # failed to process was "seen" before it could be retried.
                    status, data = await asyncio.to_thread(conn.fetch, msg_id, "(BODY.PEEK[])")
                    if status != "OK" or not data or not isinstance(data[0], tuple):
                        continue

                    raw_email = data[0][1]
                    msg = email.message_from_bytes(raw_email, policy=email.policy.default)
                    state_id = state_ids.get(msg_id) or message_state_id(msg)

                    # Dedup: skip if we already processed this email (e.g. it
                    # was handled before the examined set existed)
                    msg_message_id = msg.get("Message-ID", "").strip()
                    if msg_message_id and msg_message_id in processed_msg_ids:
                        examined_now.append(state_id)
                        continue

                    # A responder (vacation notice, ticket desk) never asks to
                    # be removed — it only repeats footer boilerplate ("to
                    # unsubscribe …"), and acting on that would unenrol (and
                    # maybe blocklist) a real prospect who is merely away.
                    # RFC 3834 headers are decisive; the reply tracker files
                    # the message as automated.
                    if automated_header_kind(msg):
                        examined_now.append(state_id)
                        continue

                    body = _extract_body(msg)
                    # Only scan the user's actual reply, NOT quoted
                    # template content (which may contain "Remove me" etc.)
                    reply_text = _extract_reply_only(body)

                    sender_email_preview = _extract_sender_email(msg)
                    subject_preview = msg.get("Subject", "(no subject)")

                    keyword_match = KEYWORD_PATTERN.search(reply_text)

                    if not keyword_match:
                        # Log if the full body HAD keywords but reply didn't
                        if KEYWORD_PATTERN.search(body):
                            logger.warning(f"[IMAP] FILTERED OUT: {sender_email_preview} "
                                  f"('{subject_preview}') — keyword only in "
                                  f"quoted/template content, not in actual reply")
                        examined_now.append(state_id)
                        continue

                    matched += 1
                    sender_email = _extract_sender_email(msg)
                    if not sender_email:
                        logger.warning(
                            f"[IMAP] Could not extract sender email from "
                            f"message {msg_id} ('{subject_preview}')"
                        )
                        examined_now.append(state_id)
                        continue

                    if sender_email in handled_this_scan:
                        examined_now.append(state_id)
                        continue

                    matched_keyword = keyword_match.group(0).strip().lower()
                    subject = msg.get("Subject", "(no subject)")
                    logger.info(f"[IMAP] Keyword match: {sender_email} ('{matched_keyword}')")

                    # Parse email date for campaign matching
                    email_date = None
                    date_str = msg.get("Date", "")
                    if date_str:
                        try:
                            dt = email.utils.parsedate_to_datetime(date_str)
                            if dt.tzinfo is not None:
                                email_date = dt.astimezone(timezone.utc)
                            else:
                                email_date = dt.replace(tzinfo=timezone.utc)
                        except Exception:
                            email_date = datetime.now(timezone.utc)
                    else:
                        email_date = datetime.now(timezone.utc)

                    # Look up subscriber in ListMonk first so we know which
                    # lists they're on before attributing to a campaign.
                    safe_email = safe_email_for_query(sender_email)
                    if not safe_email:
                        logger.warning(f"Invalid email format, skipping: {sender_email}")
                        examined_now.append(state_id)
                        continue
                    try:
                        result = await client.get_subscribers(
                            1, 1, f"subscribers.email = '{safe_email}'"
                        )
                        subscribers = (result.get("data") or {}).get("results") or []

                        if not subscribers:
                            unmatched += 1
                            logger.warning(
                                f"[IMAP] Opt-out request from {sender_email} "
                                f"('{subject}') but that address is not a ListMonk "
                                "subscriber: nothing was unsubscribed. If they were "
                                "mailed at another address, remove that one by hand."
                            )
                            examined_now.append(state_id)
                            continue

                        subscriber = subscribers[0]
                        sub_id = subscriber["id"]
                        sub_list_objs = [
                            lst for lst in subscriber.get("lists") or []
                            if isinstance(lst, dict) and lst.get("id")
                        ]
                        sub_lists = [lst["id"] for lst in sub_list_objs]
                        # The lists this request actually changes. Lists the
                        # person had already left are not "removed" by it, so
                        # "Undo All" must not re-confirm them.
                        active_lists = [
                            lst["id"] for lst in sub_list_objs
                            if (lst.get("subscription_status") or "") != "unsubscribed"
                        ]

                        # A sender the engine already handled is skipped only
                        # while that still holds (no active subscription left).
                        # Skipping on the processed set alone ignored a NEW
                        # request from someone who had been re-subscribed since
                        # (a list re-import, a manual re-add): the message was
                        # marked examined and the opt-out silently lost.
                        if sender_email in processed_emails_set and not _has_active_subscription(subscriber):
                            examined_now.append(state_id)
                            continue

                        # Attribute to the campaign that targeted one of the
                        # subscriber's lists. Attribution is bookkeeping, never
                        # a precondition: an opt-out from a known subscriber is
                        # honoured even when no campaign matches (their list
                        # was only targeted by campaigns beyond the fetched
                        # history, or they were moved off the list they were
                        # mailed on). Skipping those used to mark the request
                        # examined and lose it for good.
                        campaign = _match_campaign(
                            campaigns_list, email_date, set(sub_lists)
                        )
                        # The copy the reply most likely quotes, for the echo
                        # backstop below: the list-matched campaign, else the
                        # newest campaign sent before the reply.
                        echo_match = campaign or _match_campaign(campaigns_list, email_date, None)

                        # Backstop for quote headers the stripper does not
                        # know: if the keyword survives only in lines that
                        # repeat our own campaign copy ("reply 'remove me' to
                        # opt out"), it is our footer quoted back, not a
                        # request. A polite "Thanks, will review" from such a
                        # client used to unsubscribe (and blocklist) the lead.
                        raw_campaign = next(
                            (c for c in campaigns_list
                             if echo_match and c.get("id") == echo_match["campaign_id"]), None,
                        )
                        echo_index = await campaign_echo_index_for(
                            client, raw_campaign, echo_cache,
                        )
                        if echo_index and not KEYWORD_PATTERN.search(
                                strip_campaign_echo(reply_text, echo_index)):
                            logger.warning(
                                f"[IMAP] FILTERED OUT: {sender_email} ('{subject}') — "
                                "keyword only in quoted campaign copy, not in actual reply"
                            )
                            examined_now.append(state_id)
                            continue

                        # Unsubscribe from every list still active
                        if active_lists:
                            await client.modify_list_memberships({
                                "ids": [sub_id],
                                "action": "unsubscribe",
                                "target_list_ids": active_lists,
                                "status": "unsubscribed",
                            })
                            # Made by this engine, not a link click: the link
                            # scanner logged a second ("link") record for the
                            # same opt-out when it remembered the subscriber.
                            await add_link_seen({sub_id: active_lists})

                        # Conditionally blocklist based on user setting. The
                        # opt-out itself is done: a failed blocklist is a
                        # partial success, as in the link scanner. Raising
                        # here logged nothing, the link scan then recorded the
                        # request as a link unsubscribe and the retry skipped
                        # it as handled.
                        scan_settings = load_settings()
                        blocklisted_now = False
                        if scan_settings.get("blocklist_enabled", False):
                            try:
                                await client.blocklist_subscriber(sub_id)
                                blocklisted_now = True
                                logger.info(f"[IMAP] Blocklisted: {sender_email}")
                            except Exception as e:
                                logger.error(f"[IMAP] Failed to blocklist {sender_email}: {e}")

                        if campaign is None:
                            logger.info(
                                f"[IMAP] No list-matched campaign for '{subject}' from "
                                f"{sender_email}; unsubscribed and logged without a campaign"
                            )
                        record = {
                            "email": sender_email,
                            "name": subscriber.get("name", ""),
                            "source": "email",
                            "keyword": matched_keyword,
                            "subject": subject,
                            "message_id": msg_message_id,
                            "campaign_key": f"{email_date.year}-{email_date.month:02d}",
                            "campaign_id": campaign["campaign_id"] if campaign else None,
                            "campaign_name": campaign["campaign_name"] if campaign else "No matching campaign",
                            "matched_list_id": campaign.get("matched_list_id") if campaign else None,
                            "subscriber_id": sub_id,
                            "lists_removed": active_lists,
                            # Whether THIS engine blocklisted the subscriber:
                            # "Undo All" only lifts a blocklisting it caused.
                            "blocklisted": blocklisted_now,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            # Attributed by the current list-aware matcher: the
                            # one-time backfill below must leave it alone (it
                            # would otherwise drop a campaign-less record).
                            "reattributed_at": datetime.now(timezone.utc).isoformat(),
                        }
                        new_records.append(record)
                        examined_now.append(state_id)
                        seen_after_persist.append(msg_id)
                        handled_this_scan.add(sender_email)  # Prevent duplicates in same scan
                        processed += 1
                        action = "Unsubscribed + Blocklisted" if blocklisted_now else "Unsubscribed"
                        logger.info(f"{action}: {sender_email} (campaign: {record['campaign_name']})")

                    except Exception as e:
                        errors += 1
                        logger.error(f"Failed to process {sender_email}: {e}")

                except Exception as e:
                    errors += 1
                    logger.error(f"Failed to parse email {msg_id}: {e}")

            # Persist the new records first, then the examined set, so a crash
            # in between can only cause a re-examination (the sender is in the
            # processed set by then), never a lost unsubscribe. Messages that
            # failed transiently are in neither and are retried next scan.
            if new_records:
                await append_log(new_records)
                await mark_processed(r["email"] for r in new_records)
            await mark_examined_ids(examined_now)

            # Only the unsubscribe requests that were acted on are flagged
            # \Seen (they are done). Everything else keeps the read state the
            # human left it in: marking every examined message read used to
            # wipe the unread markers of genuine prospect replies in a shared
            # inbox.
            for mid in seen_after_persist:
                try:
                    await asyncio.to_thread(conn.store, mid, "+FLAGS", "\\Seen")
                except Exception as e:
                    logger.warning(f"[IMAP] Could not mark message {mid} as seen: {e}")

        except Exception as e:
            logger.error(f"IMAP scan error: {e}")
            errors += 1
        finally:
            _scan_running = False
            try:
                await asyncio.to_thread(conn.logout)
            except Exception:
                pass

        return {
            "scanned": scanned,
            "matched": matched,
            "processed": processed,
            "errors": errors,
            "unmatched": unmatched,
            "message": f"Scan complete: {processed} unsubscribed" + (
                f"; {unmatched} opt-out request(s) came from addresses that are "
                "not subscribers (see the log)" if unmatched else ""),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def get_stats() -> dict:
    """Return aggregate stats from the unsubscribe log."""
    records = load_log()
    total = len(records)

    now = datetime.now(timezone.utc)
    today_str = now.date().isoformat()
    week_ago = now - timedelta(days=7)

    today_count = 0
    week_count = 0
    for r in records:
        ts = r.get("timestamp", "")
        if ts.startswith(today_str):
            today_count += 1
        try:
            dt = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        # Legacy records may store naive timestamps; treat them as UTC.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= week_ago:
            week_count += 1

    # Source breakdown — records without 'source' are treated as 'email'
    link_count = sum(1 for r in records if r.get("source") == "link")
    email_count = total - link_count

    return {
        "total": total,
        "today": today_count,
        "this_week": week_count,
        "link_count": link_count,
        "email_count": email_count,
    }
