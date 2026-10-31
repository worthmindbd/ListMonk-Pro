"""IMAP Reply Tracker for ListMonk Pro Cold Outreach.

Scans the IMAP inbox for replies from cold email prospects, strips quoted
threads, categorizes sentiment (positive, neutral, auto_reply, negative, bounce),
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
from app.services.imap_helpers import (
    safe_email_for_query, extract_email_body, imap_date, fetch_state_id,
    automated_header_kind, is_bulk_mail, strip_campaign_echo,
    campaign_echo_index, campaign_echo_index_for,
)
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
    load_log, save_log, load_settings, _log_lock,
)

logger = logging.getLogger("imap_reply_tracker")

# Campaign history walks back this far (bounded) so a reply can be matched to
# the campaign that actually targeted the sender, not just the newest one.
MAX_CAMPAIGN_PAGES = 20
CAMPAIGN_PAGE_SIZE = 100

# Serialises reply scans so the 15-minute background loop and a manual
# "Scan Mailbox" click cannot process (and log) the same message twice.
_reply_scan_lock = asyncio.Lock()
_reply_scan_running = False

# Sentiment detection patterns — automated / non-human responses.
# Two groups whose union is exactly what ``classify_sentiment`` tags
# ``auto_reply``; the split only exists so the opt-in auto-blocklist feature
# can tell a dead-end machine mailbox apart from a real human who is away.
#
# _MACHINE_AUTO_PATTERNS — ticket-system auto-responses (Zendesk, Freshdesk,
# etc.), generic "we received your message" acknowledgements and explicit
# "this is an automated message" notices. Nobody reads these mailboxes.
#
# Body wording is only *evidence*: a real person writes "I'm on vacation till
# Friday, but interested — let's talk". Patterns in the overridable groups
# (_OOO_PATTERNS, _ACK_PATTERNS, _DEPARTURE_PATTERNS) yield to an explicit
# human signal (see _HUMAN_OVERRIDE_PATTERNS); headers, subject prefixes,
# automated senders and _MACHINE_AUTO_PATTERNS never do. Before that split, a
# hot lead who mentioned being away was tagged auto_reply, lost its campaign
# attribution, and — with the OOO auto-blocklist toggle on — was blocklisted.
_OOO_PATTERNS = [
    # ── English OOO / away ──────────────────────────────────────────────
    re.compile(r"out\s+of\s+(the\s+)?office", re.IGNORECASE),
    # "Automatic reply:" is the subject prefix Gmail/Outlook vacation
    # responders add — a human is away, not a machine mailbox.
    re.compile(r"auto(matic)?\s*(-|\s)?\s*reply", re.IGNORECASE),
    re.compile(r"away\s+from\s+(my\s+)?desk", re.IGNORECASE),
    re.compile(r"on\s+(annual\s+|parental\s+|maternity\s+|paternity\s+|sick\s+|medical\s+|"
               r"study\s+|holiday\s+)?leave", re.IGNORECASE),
    re.compile(r"on\s+(vacation|holiday|holidays|sabbatical)", re.IGNORECASE),
    re.compile(r"mailbox\s+is\s+full", re.IGNORECASE),
    re.compile(r"(travel+ing|on\s+the\s+road)\s+with\s+limited\s+(access|connectivity)", re.IGNORECASE),
    re.compile(r"limited\s+(access\s+to|connectivity\s+to)\s+(my\s+)?(e-?mail|inbox)", re.IGNORECASE),
    re.compile(r"return(ing)?\s+to\s+(the\s+)?office", re.IGNORECASE),
    # ── Non-English OOO (common cold-outreach locales) ──────────────────
    re.compile(r"fuera\s+de\s+(la\s+)?oficina|de\s+vacaciones", re.IGNORECASE),  # Spanish
    re.compile(r"aus\s+dem\s+büro|außer\s+haus|nicht\s+im\s+(büro|buero)|"
               r"im\s+urlaub|abwesenheitsnotiz", re.IGNORECASE),              # German
    re.compile(r"absent(e)?\s+(du\s+bureau|jusqu|à\s+partir)|en\s+cong[ée]", re.IGNORECASE),  # French
    re.compile(r"fora\s+do\s+escritório|de\s+f[ée]rias", re.IGNORECASE),     # Portuguese
    re.compile(r"fuori\s+(sede|ufficio)|in\s+ferie", re.IGNORECASE),          # Italian
    re.compile(r"buiten\s+kantoor|met\s+vakantie|ben\s+afwezig", re.IGNORECASE),  # Dutch
    re.compile(r"\bautosvar\b|fr[åa]nvarande|p[åa]\s+(ferie|semester)|"
               r"ikke\s+p[åa]\s+kontoret", re.IGNORECASE),                    # Nordic
    re.compile(r"不在办公室|休假中", re.IGNORECASE),                # Chinese
]

# Subject prefixes vacation responders add. A subject is written by the
# responder, never by a prospect typing an answer, so these do not yield to
# the human-signal override.
_OOO_SUBJECT_RE = re.compile(
    r"^\s*(automatic\s+reply|auto(matic)?[-\s]?reply|auto:|out\s+of\s+(the\s+)?office|"
    r"automatische\s+antwort|automatisch\s+antwoord|abwesenheit|autosvar|"
    r"r[ée]ponse\s+automatique|respuesta\s+autom[áa]tica|resposta\s+autom[áa]tica|"
    r"risposta\s+automatica|automaattinen\s+vastaus|odpowied[źz]\s+automatyczna|"
    r"fuera\s+de\s+(la\s+)?oficina|ooo\b)",
    re.IGNORECASE,
)

_MACHINE_AUTO_PATTERNS = [
    # ── Ticket-system auto-responses ────────────────────────────────────
    # "request received" used to match anywhere, so a human writing "Request
    # received, I'll review it this week" was a machine (and blocklisted by the
    # *safe* toggle). Only the ticket-desk phrasings remain here; the bare
    # subject form lives in _MACHINE_SUBJECT_RE.
    re.compile(r"your\s+(message|request|inquiry|enquiry|e-?mail|ticket)(\s+\(?#?\s*\w+\)?)?\s+"
               r"has\s+been\s+(received|logged|created|registered)", re.IGNORECASE),
    re.compile(r"ticket\s+(created|number\s*:?\s*#?\s*[A-Z0-9-]*\d|#\s*[A-Z0-9])", re.IGNORECASE),
    # Helpdesk signatures, not every mention of a product name: "we use
    # Zendesk today — could yours replace it?" is a prospect talking.
    re.compile(r"(delivered|powered|sent)\s+(by|via|through)\s+(zendesk|freshdesk|freshservice|"
               r"help\s*scout|zoho\s*desk|gorgias|kayako)|help\s*desk\s+ticket\s+(#|number|id|has\s+been)",
               re.IGNORECASE),
    # ── Generic auto-reply / acknowledgement ────────────────────────────
    re.compile(r"this\s+is\s+an\s+automated\s+(response|message|reply|notification)", re.IGNORECASE),
    re.compile(r"do\s+not\s+reply\s+to\s+this\s+(email|message)", re.IGNORECASE),
    re.compile(r"this\s+email\s+was\s+sent\s+from\s+a\s+notification", re.IGNORECASE),
    re.compile(r"this\s+(mail)?box\s+is\s+(not|un)\s*(monitored|attended)|"
               r"unmonitored\s+(mail)?box", re.IGNORECASE),
]

_MACHINE_SUBJECT_RE = re.compile(
    r"^\s*(\[[^\]]*\]\s*)?(re:\s*)?(request\s+received|ticket\s+(created|received|#))\b",
    re.IGNORECASE,
)

# Shared-mailbox acknowledgements. Machine kind, but body wording — so a human
# who also says "send pricing" in the same message is still a human.
_ACK_PATTERNS = [
    re.compile(r"being\s+reviewed\s+by\s+(the\s+)?.+\s*(team|support|care)", re.IGNORECASE),
    # "we'll get back to you as soon as possible" — the acknowledgement every
    # shared mailbox sends. The phrase is spelled both ways in the wild
    # ("we'll" / "we will"); only the contraction was recognised, so an
    # institutional auto-responder that spells it out ("We will get back to
    # you as soon as possible.") was never caught and fell through to the
    # positive pass, where its promotional footer read as interest.
    re.compile(r"we\s*(?:will|'ll|ll)\s+(get\s+back\s+to\s+you|be\s+in\s+touch|respond|reply)"
               r"(\s+to\s+(you|your\s+\w+))?\s+(within|as\s+soon|shortly|in\s+\d)", re.IGNORECASE),
    re.compile(r"a\s+team\s+member\s+will\s+be\s+in\s+touch", re.IGNORECASE),
    re.compile(r"we('ve|\s+have)\s+received\s+your\s+(message|e-?mail|request|inquiry|enquiry)", re.IGNORECASE),
    re.compile(r"\b(will|shall)\s+(respond|reply|get\s+back\s+to\s+you)\s+(to\s+you\s+)?within\s+\d", re.IGNORECASE),
    re.compile(r"working\s+quickly\s+to\s+respond", re.IGNORECASE),
    re.compile(r"your\s+(e-?mail|message)\s+(has\s+been|will\s+be|is\s+being)\s+forwarded", re.IGNORECASE),
    # ── Nordic auto-acknowledgements (Danish / Swedish / Norwegian) ───────
    # Institutional mailboxes (venues, municipalities, shared desks) answer in
    # the local language: "Vi vender tilbage hurtigst muligt" is the exact
    # Danish counterpart of "we will get back to you as soon as possible".
    # The English rule above never sees these, so the acknowledgement used to
    # reach the positive pass carrying a marketing footer.
    re.compile(r"vi\s+vender\s+tilbage\s+hurtigst\s+muligt", re.IGNORECASE),
    re.compile(r"vi\s+[åa]terkommer\s+(s[åa]\s+snart\s+som\s+m[öo]jligt|snabbt)", re.IGNORECASE),
    re.compile(r"vi\s+kommer\s+tilbake\s+(s[åa]\s+snart\s+som\s+mulig|fort)", re.IGNORECASE),
]

# Departure notices ("Jane is no longer with Acme, please contact …"). The
# address is a dead end for outreach, so these are machine kind.
_DEPARTURE_PATTERNS = [
    re.compile(r"\bno\s+longer\s+(with|at|employed|working|part\s+of)\b", re.IGNORECASE),
    re.compile(r"\bhas\s+left\s+(the\s+company|the\s+organi[sz]ation|[A-Z]\w+)", re.IGNORECASE),
    re.compile(r"\bnicht\s+mehr\s+(bei|im\s+unternehmen)|\bne\s+fait\s+plus\s+partie", re.IGNORECASE),
]

# Explicit human signals an auto-responder never writes. Deliberately narrow:
# vacation notices say "call me on my mobile" and "to schedule a meeting,
# contact my assistant", so calls/meetings/pricing are not here.
_HUMAN_OVERRIDE_PATTERNS = [
    re.compile(r"\binterested\b", re.IGNORECASE),
    re.compile(r"\bsounds\s+(good|great|interesting|promising)\b", re.IGNORECASE),
    re.compile(r"\blet'?s\s+(talk|chat|connect|meet|hop\s+on|do\s+it|schedule|set\s+up|book|find\s+a\s+time)\b", re.IGNORECASE),
    re.compile(r"\byes\s*,?\s+(please|let'?s|i'?d|we'?d|definitely|absolutely)\b", re.IGNORECASE),
    re.compile(r"\btell\s+me\s+more\b", re.IGNORECASE),
    re.compile(r"\bhappy\s+to\s+(connect|chat|talk|hop\s+on)\b", re.IGNORECASE),
    re.compile(r"\b(i|we)('d|\s+would)\s+love\s+to\b", re.IGNORECASE),
    re.compile(r"\bremove\s+me\b|\btake\s+me\s+off\b|\bstop\s+(e-?mailing|sending|contacting)\b", re.IGNORECASE),
    re.compile(r"\bno\s+thanks?\b|\bno\s+thank\s+you\b", re.IGNORECASE),
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
#
# "no-reply" is matched as a token anywhere in the local part
# (messages-noreply@linkedin.com, drive-shares-dm-noreply@google.com): only a
# leading one was recognised, so those notifications reached the positive pass
# and were auto-flagged as Hot Leads. The role words must be the whole local
# part (or a separated token), so a human "news.editor@" / "alerts-team@" is
# no longer a machine.
_AUTOMATED_SENDER_RE = re.compile(
    r"(^|[-_.+])(no[-_]?reply|do[-_]?not[-_]?reply|donotreply)([-_.+]|$)"
    r"|^(auto[-_]?(generated|reply|responder)|newsletters?|news)([+]|$)"
    r"|(^|[-_.])(notifications?|alerts?)([+]|$)",
    re.IGNORECASE,
)
# Helpdesk platforms sending on a customer's behalf.
_HELPDESK_DOMAIN_RE = re.compile(
    r"(^|\.)(zendesk|freshdesk|helpscout|zohodesk|gorgias|kayako)\.(com|net|io)$",
    re.IGNORECASE,
)

# Negative patterns come in two strengths. A *strong* refusal is decisive
# whatever else the reply says ("No thanks, how much would it cost anyway?").
# A *soft* decline ("we don't need a demo", "we'll keep you in mind") yields to
# an explicit request in the same reply: "I don't need a demo, just send
# pricing" is a lead, and reading it as a refusal buried it.
_NEGATIVE_PATTERNS = [
    re.compile(r"not\s+interested", re.IGNORECASE),
    re.compile(r"\b(un|dis)interested\b", re.IGNORECASE),
    re.compile(r"unsubscribe", re.IGNORECASE),
    re.compile(r"remove\s+me", re.IGNORECASE),
    re.compile(r"take\s+me\s+off", re.IGNORECASE),
    re.compile(r"\bstop\s+(e-?mailing|sending|contacting|messaging|spamming)\b", re.IGNORECASE),
    re.compile(r"do\s+not\s+(email|contact)", re.IGNORECASE),
    re.compile(r"\b(don'?t|never)\s+(e-?mail|contact|message|write\s+to|reach\s+out\s+to)\s+(me|us)\b", re.IGNORECASE),
    re.compile(r"\bno\s+thanks\b|\bno\s*,?\s+thank\s+you\b", re.IGNORECASE),
    # A reply that is nothing but "No." / "Nope" (signature lines may follow).
    re.compile(r"\A\s*(no|nope|nah|no\s+way)\s*[.!]*\s*(\n|\Z)", re.IGNORECASE),
    re.compile(r"\bnot\s+for\s+(us|me)\b", re.IGNORECASE),
    re.compile(r"\b(we'?re|we\s+are|i'?m|i\s+am)\s+all\s+set\b(?!\s+(for|to|on|with)\b)", re.IGNORECASE),
    re.compile(r"\b(we'?ll|i'?ll|we\s+will|i\s+will)\s+pass\b", re.IGNORECASE),
    re.compile(r"wrong\s+person", re.IGNORECASE),
    re.compile(r"cease\s+and\s+desist", re.IGNORECASE),
    re.compile(r"report\s+(as\s+)?spam|\bthis\s+is\s+spam\b|\bspamming\s+me\b", re.IGNORECASE),
    re.compile(r"\bhow\s+did\s+you\s+get\s+my\s+(e-?mail|address|contact|details|number)\b", re.IGNORECASE),
    re.compile(r"\bnot\s+the\s+right\s+(time|person|moment)\b", re.IGNORECASE),
    re.compile(r"\bpass\s+(on\s+this|for\s+now)\b", re.IGNORECASE),
]

# Polite declines: no blunt "not interested", but zero interest either.
# Without these a courteous "no" ("we are not exploring this type of
# collaboration…") matches nothing and would be misread as a positive reply.
_SOFT_NEGATIVE_PATTERNS = [
    re.compile(r"\bnot\s+(currently\s+|at\s+this\s+time\s+)?(exploring|seeking|pursuing|looking\s+(for|to)|in\s+the\s+market)", re.IGNORECASE),
    re.compile(r"\bnot\s+(a|the)\s+(good\s+|great\s+|right\s+)?fit\b", re.IGNORECASE),
    re.compile(r"\bdon'?t\s+think\s+(this|it|we|that)(\s+is|'s|\s+are|'re|\s+would\s+be)?\s+"
               r"(a\s+|the\s+)?(good\s+|great\s+|right\s+)?(fit|match)\b", re.IGNORECASE),
    re.compile(r"\b(no|don'?t|do\s+not)\s+(current\s+|immediate\s+)?need\b", re.IGNORECASE),
    re.compile(r"\b(no|(don'?t|do\s+not)\s+have\s+(a\s+|the\s+|any\s+)?)budget\b", re.IGNORECASE),
    re.compile(r"\balready\s+(have|work\s+with|use|using|got|partner\s+with)\s+(a\s+|an\s+|our\s+|the\s+|some\s+)?"
               r"(\w+\s+)?(vendor|provider|partner|agency|solution|supplier|tool|team|platform|company|someone)\b",
               re.IGNORECASE),
    re.compile(r"\b(have\s+)?sav(?:ed|e)\s+your\s+(contact|details|information)", re.IGNORECASE),
    re.compile(r"\b(keep|keeping)\s+(you|your\s+\w+)\s+(in\s+mind|on\s+file)\b", re.IGNORECASE),
    re.compile(r"\b(will|would|'ll)\s+(certainly\s+|definitely\s+|be\s+sure\s+to\s+)?(get|be)\s+in\s+touch\s+if\s+(the\s+)?(situation|needs?|circumstances?)", re.IGNORECASE),
    re.compile(r"\b(wish\s+you\s+(great\s+|much\s+|every\s+|continued\s+)?success|best\s+of\s+luck)\b", re.IGNORECASE),
]

# Every keyword is word-bounded: as bare substrings "yes" matched
# "yesterday"/"eyes", "cost" matched "costume"/"Costa", "interested" matched
# "uninterested", and "let's" matched "outlets" — each one auto-promoting an
# ordinary reply to a Hot Lead.
#
# _STRONG_POSITIVE_PATTERNS are explicit requests; they also beat a soft
# decline in the same reply. _POSITIVE_PATTERNS are weaker interest cues.
_STRONG_POSITIVE_PATTERNS = [
    re.compile(r"\bsounds\s+(good|great|interesting|promising)\b", re.IGNORECASE),
    re.compile(r"\binterested\b", re.IGNORECASE),
    re.compile(r"\blet'?s\s+(talk|chat|connect|meet|hop\s+on|do\s+it|go\s+ahead|schedule|set\s+up|book|"
               r"find\s+a\s+time)\b", re.IGNORECASE),
    re.compile(r"\bsend\s+(me\s+|us\s+)?(over\s+)?(a\s+|an\s+|the\s+|some\s+|your\s+)?(more\s+)?"
               r"(info|information|details|pricing|prices|deck|proposal|quote|brochure|"
               r"case\s+stud(?:y|ies)|examples?|samples?|portfolio|rates?)\b", re.IGNORECASE),
    re.compile(r"\bsend\s+(it|that|them|this)\s+over\b", re.IGNORECASE),
    re.compile(r"\byes\s*,?\s+please\b", re.IGNORECASE),
    re.compile(r"happy\s+to\s+(connect|chat|talk|hop\s+on)", re.IGNORECASE),
    re.compile(r"tell\s+me\s+more", re.IGNORECASE),
    re.compile(r"\b(i|we)('d|\s+would)\s+love\s+to\b", re.IGNORECASE),
]

_POSITIVE_PATTERNS = [
    re.compile(r"\bschedule\s+(a\s+)?(call|time|demo)\b", re.IGNORECASE),
    re.compile(r"\b(set\s+up|book|arrange)\s+(a\s+|an\s+|some\s+)?(quick\s+|short\s+|brief\s+)?"
               r"(call|meeting|chat|demo|time|intro)\b", re.IGNORECASE),
    re.compile(r"\bcalend(ar|ly)\b", re.IGNORECASE),
    re.compile(r"\b(pricing|how\s+much|costs?)\b", re.IGNORECASE),
    re.compile(r"\byes\b(\s+please)?", re.IGNORECASE),
    # An answer that opens with assent: "Sure, send over a deck."
    re.compile(r"\A\s*(sure|absolutely|definitely|of\s+course|yeah|yep|why\s+not)\b", re.IGNORECASE),
    re.compile(r"\b(your|what'?s\s+your|what\s+is\s+your)\s+availability\b|"
               r"\bwhen\s+(are|would)\s+you\s+(be\s+)?(free|available)\b|"
               r"\bare\s+you\s+(free|available)\b", re.IGNORECASE),
    re.compile(r"\b(works|work)\s+for\s+(me|us)\b|\bthat\s+works\b", re.IGNORECASE),
    re.compile(r"give\s+me\s+a\s+call|\b(call|ring|phone)\s+me\b", re.IGNORECASE),
    re.compile(r"\bopen\s+to\s+(a\s+)?(call|chat|conversation|discussing|learning|exploring)", re.IGNORECASE),
    re.compile(r"\bmore\s+(info|information|details)\b", re.IGNORECASE),
    re.compile(r"forwarded\s+to\s+our|\b(forwarded|passed|sent)\s+(this|it|your\s+(e-?mail|message|note))\s+"
               r"(on\s+|along\s+)?to\s+(our|my|the)\b", re.IGNORECASE),
]

# A positive keyword that sits inside a third-person offer is the sender's
# own marketing copy, not interest in our proposal. Institutional
# auto-responders append a promotional footer ("If you are interested in
# renting a venue, please visit …"), and matching "interested" in that
# sentence turned an acknowledgement into a Hot Lead. The offer owns the rest
# of its sentence, so a second keyword there ("… our pricing …") is part of
# the pitch too.
_CONDITIONAL_OFFER_RE = re.compile(
    r"(if\s+you(?:'re|\s+are|\s+would\s+be)\s+(?:interested|keen)\b"
    r"|anyone\s+(?:and\s+everyone\s+)?(?:else\s+)?interested\b"
    r"|interested\s+parties\b"
    r"|should\s+you\s+be\s+interested\b)"
    r"[^.!?]*[.]?",
    re.IGNORECASE,
)

# A first-person statement of interest is the prospect's own and always
# counts, even when the sentence also carries a third-person offer ("We are
# interested — if you are interested too, let's talk").
_FIRST_PERSON_PREFIX_RE = re.compile(
    r"\b(i|we|our\s+\w+|my)\b\s*(?:'m|'re|am|are)?\s*"
    r"(?:very\s+|really\s+|quite\s+)?$",
    re.IGNORECASE,
)


def _inside_conditional_offer(text: str, start: int, end: int) -> bool:
    """True when the text span lies inside a third-person/conditional offer."""
    prefix = text[max(0, start - 60):start]
    if _FIRST_PERSON_PREFIX_RE.search(prefix):
        return False  # the prospect's own interest, not the sender's pitch
    return any(
        m.start() <= start and end <= m.end()
        for m in _CONDITIONAL_OFFER_RE.finditer(text)
    )


def _first_match(patterns, text: str) -> bool:
    """True when any pattern matches outside a third-person marketing offer."""
    for pat in patterns:
        for m in pat.finditer(text):
            if _inside_conditional_offer(text, m.start(), m.end()):
                # The keyword belongs to a marketing offer the sender wrote about
                # their own product ("if you are interested in renting a
                # venue…"), so it says nothing about interest in our offer.
                continue
            return True
    return False


# Typographic apostrophes (iOS/macOS smart punctuation is on by default) are
# folded to ASCII before matching: every pattern above spells "let's",
# "don't", "we'll" with a plain apostrophe, so "Let’s talk" was invisible.
_SMART_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'"})


def _ascii_apostrophes(text: str) -> str:
    return (text or "").translate(_SMART_APOSTROPHES)


# Display noise left behind by mail clients inside the plain-text part.
_IMAGE_PLACEHOLDER_RE = re.compile(r"\[image:\s*[^\]]*\]", re.IGNORECASE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def _auto_reply_kind(
    subject: str, reply_text: str, from_email: str = "",
    header_kind: Optional[str] = None,
) -> Optional[str]:
    """Distinguish the two kinds of automated reply, or return ``None``.

    Returns:
        ``'machine'`` — no-reply/notification senders, ticket systems,
        explicit "automated message" notices and departure notices: a
        mailbox no human reads (for this prospect).
        ``'ooo'``     — out-of-office / away notices written by a real
        (temporarily away) human who will read the mail on return.
        ``None``      — not an automated reply.

    ``header_kind`` is :func:`imap_helpers.automated_header_kind` of the
    message (RFC 3834 ``Auto-Submitted`` and friends) — the most reliable
    signal, so it is decisive. Body wording alone yields to an explicit
    human signal (see ``_HUMAN_OVERRIDE_PATTERNS``).

    The union of both pattern groups plus the automated-sender check is
    exactly what :func:`classify_sentiment` tags ``auto_reply``, so this
    function can never disagree with the feed's sentiment badge.
    """
    subject, reply_text = _ascii_apostrophes(subject), _ascii_apostrophes(reply_text)
    combined = f"{subject}\n{reply_text}"

    # Machine wording wins over away wording: a ticket system that happens to
    # mention "out of office", or a no-reply sender carrying an away notice,
    # is still a mailbox no human reads.
    for pat in _MACHINE_AUTO_PATTERNS:
        if pat.search(combined):
            return "machine"
    if _MACHINE_SUBJECT_RE.search(subject):
        return "machine"

    sender = (from_email or "").strip().lower()
    local_part, _, domain = sender.partition("@")
    if _AUTOMATED_SENDER_RE.search(local_part) or _HELPDESK_DOMAIN_RE.search(domain):
        return "machine"

    if header_kind == "auto_generated":
        return "machine"
    if header_kind == "auto_replied":
        # A responder answered: a ticket desk / departure notice is a dead
        # end, anything else is a vacation notice from a real person.
        if any(p.search(combined) for p in _ACK_PATTERNS + _DEPARTURE_PATTERNS):
            return "machine"
        return "ooo"

    if _OOO_SUBJECT_RE.search(subject):
        return "ooo"

    # Body-only evidence from here on: a person who writes "I'm on leave this
    # week, but yes please — let's talk Monday" is answering, not away.
    ack = any(p.search(combined) for p in _ACK_PATTERNS + _DEPARTURE_PATTERNS)
    ooo = not ack and any(p.search(combined) for p in _OOO_PATTERNS)
    if not (ack or ooo):
        return None
    if _first_match(_HUMAN_OVERRIDE_PATTERNS, reply_text):
        return None
    return "machine" if ack else "ooo"


def classify_sentiment(
    subject: str, reply_text: str, from_email: str = "",
    header_kind: Optional[str] = None,
) -> str:
    """Classify the sentiment of a prospect reply.

    Returns: 'positive' | 'auto_reply' | 'negative' | 'neutral' | 'bounce'

    ``from_email`` (optional) lets the delivery-failure and automated-sender
    checks run before any keyword: a bounce is never a human sentiment, and a
    no-reply sender's notification text must not inherit keyword matches.
    ``header_kind`` (optional) is the message's automation-header verdict
    (:func:`imap_helpers.automated_header_kind`); stored records keep it as
    ``auto_header`` so re-classification reaches the same answer.
    """
    subject, reply_text = _ascii_apostrophes(subject), _ascii_apostrophes(reply_text)
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

    # 2. Out of Office / Auto-reply check — both machine acknowledgements and
    #    human away notices (see _auto_reply_kind for the distinction).
    if _auto_reply_kind(subject, reply_text, from_email, header_kind) is not None:
        return "auto_reply"

    # 3. Negative / Unsubscribe check — strong refusals are decisive.
    for pat in _NEGATIVE_PATTERNS:
        if pat.search(reply_text) or pat.search(subject):
            return "negative"

    # 4. Positive / Warm Lead check — an explicit request beats a soft decline.
    if _first_match(_STRONG_POSITIVE_PATTERNS, reply_text):
        return "positive"

    # 5. Polite declines.
    for pat in _SOFT_NEGATIVE_PATTERNS:
        if pat.search(reply_text) or pat.search(subject):
            return "negative"

    # 6. Weaker interest cues.
    if _first_match(_POSITIVE_PATTERNS, reply_text):
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
    subscriber: Optional[dict] = None,
) -> tuple[Optional[int], Optional[str]]:
    """Find the campaign a prospect reply belongs to.

    Matching order:
      1. Subject match against the campaign history (walked back to cover the
         reply date, bounded by MAX_CAMPAIGN_PAGES).
      2. List-aware match: the newest campaign created on or before the reply
         that targeted one of the sender's lists.

    Recency alone is never used — attributing a reply to an unrelated campaign
    is worse than leaving it unattributed. Pass a pre-fetched ``campaigns``
    list to avoid re-walking the history per message, and an already looked
    up ``subscriber`` to skip the per-reply ListMonk lookup.
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
    if subscriber is None:
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
        subscriber = subs[0]

    sub_list_ids = {
        l.get("id") for l in (subscriber.get("lists") or []) if l.get("id")
    }
    match = _match_campaign(campaigns, reply_date, sub_list_ids)
    if match:
        return match.get("campaign_id"), match.get("campaign_name")
    return None, None


async def _lookup_subscriber(
    listmonk: ListMonkClient, from_email: str, cache: dict,
) -> tuple[Optional[dict], bool]:
    """Resolve a sender to its ListMonk subscriber, once per scan.

    Returns ``(subscriber, ok)``: ``ok`` is False when ListMonk could not be
    asked (the caller must defer the message, not discard it), and
    ``subscriber`` is None when the sender is genuinely unknown.
    """
    if from_email in cache:
        return cache[from_email], True
    safe_email = safe_email_for_query(from_email)
    if not safe_email:
        cache[from_email] = None
        return None, True
    try:
        res = await listmonk.get_subscribers(
            1, 1, f"subscribers.email = '{safe_email}'",
        )
        subs = (res.get("data") or {}).get("results") or []
    except Exception as e:
        logger.warning(f"[ReplyTracker] Subscriber lookup failed for {from_email}: {e}")
        return None, False
    cache[from_email] = subs[0] if subs else None
    return cache[from_email], True


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


def _auto_leads_enabled() -> bool:
    """``auto_tag_leads`` (reply_settings.json): promote positive replies to
    ``lead`` automatically. It used to be stored and validated but never
    read, so switching it off changed nothing."""
    return bool(load_settings().get("auto_tag_leads", True))


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


def _clear_campaign_attribution(record: dict) -> bool:
    """Drop stored campaign attribution when a reply becomes an auto-reply.

    The scan never attributes automated responses (OOO desks, ticket systems)
    to a campaign, so ``by_campaign``/``campaign_options`` rely on auto-replies
    carrying no ``campaign_id``. A record re-tagged ``auto_reply`` by a later
    classifier run (or written before that rule existed) would otherwise keep
    inflating per-campaign reply charts whose reply-rate KPI excludes it.

    Returns True when the attribution was changed.
    """
    changed = record.get("campaign_id") is not None or record.get("campaign_name")
    record["campaign_id"] = None
    record["campaign_name"] = None
    return bool(changed)


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
        header_kind = automated_header_kind(msg)
        sentiment = classify_sentiment(
            msg.get("Subject", ""), reply_only, repair_from, header_kind,
        )
        repairs[msg_id] = {
            "full_body": reply_only,
            "body_preview": reply_only[:200] + ("..." if len(reply_only) > 200 else ""),
            "sentiment": sentiment,
            "auto_header": header_kind,
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
    auto_leads = _auto_leads_enabled()
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
            if fix.get("auto_header"):
                r["auto_header"] = fix["auto_header"]
            if fix["sentiment"] == "positive":
                if auto_leads and r.get("status") == "unread":
                    r["status"] = "lead"
                    r["status_source"] = STATUS_SOURCE_AUTO
            else:
                _demote_auto_lead(r)
                if fix["sentiment"] == "auto_reply":
                    _clear_campaign_attribution(r)
            changed += 1
        if changed:
            if drop_ids:
                records = [r for r in records if r.get("message_id") not in drop_ids]
            save_log(records)
    return changed


async def reclassify_stored_sentiments(
    echo_indexes: Optional[dict] = None,
    reattribute: Optional[list] = None,
) -> int:
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

    The stored body is first passed through the current quote stripper (and,
    when ``echo_indexes`` holds the record's campaign, the campaign-echo
    filter): older versions kept Outlook / non-English quoted threads, so our
    own campaign copy was classified as the prospect's words. A body only
    ever shrinks this way — it is never replaced by an empty one.

    ``reattribute`` (optional list) receives the ids of records that moved
    from ``auto_reply`` to a human sentiment: their campaign attribution was
    dropped when they were tagged automated and the caller re-matches them.
    """
    echo_indexes = echo_indexes or {}
    auto_leads = _auto_leads_enabled()
    async with _log_lock:
        records = load_log()
        changed = 0
        drop_ids: set[str] = set()
        for r in records:
            body = (r.get("full_body") or "").strip()
            if not body:
                continue  # empty bodies are the repair pass's job
            cleaned = _tidy_reply_text(_extract_reply_only(body)) or body
            index = echo_indexes.get(r.get("campaign_id"))
            classify_text = strip_campaign_echo(cleaned, index) if index else cleaned
            if r.get("echo_only"):
                # The scan found nothing but our quoted campaign copy; the
                # stored body is kept for display only.
                classify_text = ""
            elif classify_text:
                cleaned = classify_text
            body_changed = cleaned != body
            if body_changed:
                r["full_body"] = cleaned
                r["body_preview"] = cleaned[:200] + ("..." if len(cleaned) > 200 else "")
                body = cleaned
            sentiment = classify_sentiment(
                r.get("subject", ""), classify_text, r.get("from_email", ""),
                r.get("auto_header"),
            )
            if sentiment == r.get("sentiment"):
                changed += int(body_changed)
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
            previous = r.get("sentiment")
            r["sentiment"] = sentiment
            if sentiment == "positive":
                if auto_leads and r.get("status") == "unread":
                    r["status"] = "lead"
                    r["status_source"] = STATUS_SOURCE_AUTO
            else:
                # The old unknown-means-positive default hot-flagged replies
                # like a lone "hi". Correcting the badge is not enough: the
                # auto-promoted lead status has to go with it, or the card
                # keeps its "Hot Lead Flagged" state and lead-filter entry.
                _demote_auto_lead(r)
                if sentiment == "auto_reply":
                    _clear_campaign_attribution(r)
            if (previous == "auto_reply" and sentiment != "auto_reply"
                    and reattribute is not None and r.get("campaign_id") is None):
                reattribute.append(r.get("id"))
            changed += 1
        if changed:
            if drop_ids:
                records = [r for r in records if r.get("message_id") not in drop_ids]
            save_log(records)
    return changed


async def reattribute_replies(
    listmonk: ListMonkClient, reply_ids: list, campaigns: list[dict],
) -> int:
    """Restore campaign attribution for replies that stopped being auto-replies.

    Matching runs outside the log lock (it may call ListMonk); the write-back
    re-reads the log and only fills records that are still unattributed and
    still human, so a concurrent edit is never overwritten.
    """
    wanted = {str(i) for i in reply_ids if i}
    if not wanted or not campaigns:
        return 0
    found: dict[str, tuple] = {}
    for r in load_log():
        if str(r.get("id")) not in wanted:
            continue
        reply_date = None
        try:
            dt = email.utils.parsedate_to_datetime(r.get("date") or "")
            reply_date = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
        camp_id, camp_name = await match_campaign_for_reply(
            listmonk, r.get("subject", ""), r.get("from_email", ""),
            reply_date, campaigns=campaigns,
        )
        if camp_id:
            found[str(r.get("id"))] = (camp_id, camp_name)
    if not found:
        return 0
    async with _log_lock:
        records = load_log()
        updated = 0
        for r in records:
            hit = found.get(str(r.get("id")))
            if hit and r.get("campaign_id") is None and r.get("sentiment") != "auto_reply":
                r["campaign_id"], r["campaign_name"] = hit
                updated += 1
        if updated:
            save_log(records)
    return updated


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


async def blocklist_reply_sender(listmonk: ListMonkClient, email: str) -> dict:
    """Blocklist the ListMonk subscriber behind a sender address.

    Shared by the Replies API's manual blocklist actions and the reply
    scanner's opt-in auto-blocklisting, so both resolve the email
    server-side — that keeps the address out of client-built ListMonk query
    strings and validates it with the same helper the scanners use, so a
    crafted Reply-To address cannot inject query syntax.

    Returns an error dict (with ``error`` key) instead of raising
    HTTPException so bulk callers can distinguish client errors from
    server errors in their partial-failure report.
    """
    safe_email = safe_email_for_query((email or "").strip())
    if not safe_email:
        return {"error": "Reply has no valid sender email", "email": email}

    try:
        result = await listmonk.get_subscribers(1, 1, f"subscribers.email = '{safe_email}'")
    except Exception as e:
        logger.error(f"Subscriber lookup failed for {email}: {e}")
        return {"error": f"Subscriber lookup failed: {e}", "email": email}
    subscribers = (result.get("data") or {}).get("results") or []
    if not subscribers:
        return {"error": f"No ListMonk subscriber found for {email}", "email": email}

    subscriber_id = subscribers[0]["id"]
    try:
        await listmonk.blocklist_subscriber(subscriber_id)
    except Exception as e:
        logger.error(f"Blocklist failed for {email}: {e}")
        return {"error": f"Blocklist failed: {e}", "email": email}

    return {"status": "ok", "subscriber_id": subscriber_id, "email": email}


async def scan_and_track_replies(
    listmonk: ListMonkClient,
    days_back: int = 14
) -> dict:
    """Connect to IMAP and scan for prospect replies.

    Serialised with a module lock: the 15-minute background loop and the
    manual "Scan Mailbox" endpoint both call this, and two overlapping scans
    would load the same processed-ID set and log duplicate replies.
    """
    global _reply_scan_running
    if _reply_scan_running:
        return {
            "error": None,
            "scanned": 0,
            "new_replies": 0,
            "positive": 0,
            "negative": 0,
            "neutral": 0,
            "auto_reply": 0,
            "bounce": 0,
            "reclassified": 0,
            "auto_blocklisted": 0,
            "not_reply": 0,
            "deferred": 0,
            "message": "Reply scan already in progress",
        }

    async with _reply_scan_lock:
        _reply_scan_running = True
        try:
            return await _scan_and_track_replies_impl(listmonk, days_back)
        finally:
            _reply_scan_running = False


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
            "auto_blocklisted": 0,
            "not_reply": 0,
            "deferred": 0,
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
        "auto_blocklisted": 0,
        # Inbox mail that is not an answer to our outreach (newsletters,
        # receipts, platform notifications from strangers): skipped, never
        # stored — it used to land in the feed and inflate the reply rate.
        "not_reply": 0,
        # Messages left for the next scan because ListMonk could not be asked
        # whether the sender is a subscriber.
        "deferred": 0,
    }

    # Opt-in auto-blocklist toggles (reply_settings.json), read once per
    # scan. ``blocked_emails`` stops a chatty ticket system from triggering
    # one lookup + blocklist per message within the same scan.
    block_toggles = load_settings()
    auto_block_machine = bool(block_toggles.get("auto_blocklist_machine"))
    auto_block_ooo = bool(block_toggles.get("auto_blocklist_ooo"))
    auto_leads = bool(block_toggles.get("auto_tag_leads", True))
    blocked_emails: set[str] = set()
    # Per-scan caches: one subscriber lookup per sender, one campaign /
    # template fetch per campaign for echo stripping.
    sub_cache: dict = {}
    echo_cache: dict = {}

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

    # Stored replies whose body is still empty need their message again (the
    # repair pass below); everything else already processed does not.
    repair_ids = _repairable_record_ids(load_log())

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
                # Identity-header peek first: the scan runs every 15 minutes
                # over the same window, and downloading all 200 full messages
                # (attachments included) each time re-fetched mail that was
                # processed long ago. Only unprocessed messages, and processed
                # ones awaiting a body repair, are downloaded.
                state_id = fetch_state_id(mail, num)
                if state_id and state_id in processed_ids and state_id not in repair_ids:
                    continue
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
    # Only campaign bodies already in the listing feed the stored-record echo
    # filter: fetching every historical campaign each scan would be costly.
    listing_echo = {
        c.get("id"): campaign_echo_index(c.get("body"), c.get("altbody"))
        for c in campaigns if c.get("id") and isinstance(c.get("body"), str)
    }
    reattribute_ids: list = []
    try:
        stats["reclassified"] = await reclassify_stored_sentiments(
            echo_indexes=listing_echo, reattribute=reattribute_ids,
        )
        if stats["reclassified"]:
            logger.info(
                f"[ReplyTracker] Re-classified {stats['reclassified']} stored "
                "reply record(s) with the current sentiment rules"
            )
        if reattribute_ids:
            restored = await reattribute_replies(listmonk, reattribute_ids, campaigns)
            if restored:
                logger.info(
                    f"[ReplyTracker] Restored campaign attribution on {restored} "
                    "reply record(s) no longer read as automated"
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
        # RFC 3834 automation headers outrank any body wording.
        header_kind = automated_header_kind(msg)

        sentiment = classify_sentiment(subject, reply_only, from_email, header_kind)

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

        # Only answers to our outreach belong in the feed. Every other message
        # in the inbox (newsletters, receipts, LinkedIn/Google notifications)
        # used to be stored as a "reply": notifications were hot-flagged as
        # leads and every newsletter inflated the reply rate. Kept: mail whose
        # subject matches one of our campaigns, or whose sender is a ListMonk
        # subscriber. Newsletter / list traffic is dropped up front.
        if is_bulk_mail(msg):
            stats["not_reply"] += 1
            if state_id:
                new_msg_ids.append(state_id)
            continue
        clean_subj = _clean_subject_for_matching(subject)
        subscriber = None
        if not any(_subject_matches(clean_subj, c) for c in campaigns):
            subscriber, lookup_ok = await _lookup_subscriber(listmonk, from_email, sub_cache)
            if not lookup_ok:
                # ListMonk unreachable: not knowing is not "unknown sender".
                # Leave the message unprocessed so the next scan decides.
                stats["deferred"] += 1
                continue
            if subscriber is None:
                stats["not_reply"] += 1
                if state_id:
                    new_msg_ids.append(state_id)
                continue

        # Match campaign (history was fetched once for the whole scan).
        camp_id, camp_name = await match_campaign_for_reply(
            listmonk, subject, from_email, reply_date, campaigns=campaigns,
            subscriber=subscriber,
        )

        # Drop any of our own campaign copy the quote stripper missed (an
        # unrecognised client's quote header), then classify what is left.
        echo_only = False
        if camp_id:
            campaign = next((c for c in campaigns if c.get("id") == camp_id), None)
            echo_index = await campaign_echo_index_for(listmonk, campaign, echo_cache)
            cleaned = strip_campaign_echo(reply_only, echo_index)
            if cleaned != reply_only:
                # Nothing but our own copy: classify the empty answer, but keep
                # the text for display (an empty body would look repairable
                # and be re-downloaded every scan). ``echo_only`` tells
                # re-classification to reach the same verdict.
                echo_only = not cleaned
                if cleaned:
                    reply_only = cleaned
                sentiment = classify_sentiment(
                    subject, cleaned, from_email, header_kind,
                )

        # Automated responses (OOO, ticket confirmations, generic auto-replies)
        # are never genuine prospect engagement — skip attribution entirely so
        # they don't receive a campaign tag or inflate per-campaign charts.
        auto_kind = None
        if sentiment == "auto_reply":
            camp_id, camp_name = None, None
            auto_kind = _auto_reply_kind(subject, reply_only, from_email, header_kind)

        # Note: Replies without campaign attribution are still stored with
        # campaign_id=null. While ideally we'd skip non-campaign emails, doing
        # so risks losing valid replies when matching fails due to incomplete
        # data (deleted campaigns, changed subjects, etc.). The neutral sentiment
        # default ensures these aren't falsely flagged as hot leads, and
        # campaign_name stays null so the feed labels them "Unattributed"
        # instead of inventing a campaign they never belonged to.
        # The gate above only drops mail from senders who are not subscribers
        # *and* whose subject matches no campaign, so a subscriber's reply to a
        # deleted or renamed campaign is still kept (unattributed).
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
            "status": "lead" if sentiment == "positive" and auto_leads else "unread",
            # Provenance for the auto-promotion above, so a later sentiment
            # correction can undo it without touching a human's status choice.
            "status_source": STATUS_SOURCE_AUTO,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        # Opt-in auto-blocklisting of automated replies. The toggles decide
        # which kind applies: machine mailboxes (no-reply desks, ticket
        # systems) are dead ends for outreach; out-of-office notices belong
        # to real humans and only get blocked when the harsher toggle is on.
        # A failed blocklist never loses the reply — the record still feeds
        # the page for manual triage.
        if header_kind:
            # Kept so re-classification of the stored body reaches the same
            # verdict without the original headers.
            record["auto_header"] = header_kind
        if echo_only:
            record["echo_only"] = True

        if sentiment == "auto_reply" and (
            (auto_kind == "machine" and auto_block_machine)
            or (auto_kind == "ooo" and auto_block_ooo)
        ) and from_email not in blocked_emails:
            blocked_emails.add(from_email)
            res = await blocklist_reply_sender(listmonk, from_email)
            if "error" in res:
                logger.warning(
                    f"[ReplyTracker] Auto-blocklist failed for {from_email}: "
                    f"{res['error']}"
                )
            else:
                record["auto_blocklisted"] = True
                stats["auto_blocklisted"] += 1

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
        await mark_processed_message_ids(new_msg_ids)

    return stats
