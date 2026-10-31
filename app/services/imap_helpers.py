"""Shared IMAP helpers: email validation, IMAP date formatting, body extraction,
and the idempotency keys the scanners use instead of IMAP ``\\Seen``."""

import email
import email.policy
import hashlib
import logging
import re
from datetime import datetime
from html.parser import HTMLParser

logger = logging.getLogger("imap_helpers")

_ENG_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w.-]+\.\w+$")


def safe_email_for_query(email_addr: str) -> str | None:
    """Validate email format and escape for SQL query interpolation."""
    # fullmatch, not match: ``$`` also matches before a trailing newline, so
    # "a@b.com\n" used to pass and reach the query with the newline intact.
    if not _EMAIL_RE.fullmatch(email_addr or ""):
        return None
    return email_addr.replace("'", "''")


def imap_date(dt: datetime) -> str:
    """Format a datetime as an IMAP SINCE date with English month names."""
    return f"{dt.day:02d}-{_ENG_MONTHS[dt.month - 1]}-{dt.year}"


# ─── Idempotency keys ────────────────────────────────────────────────────────
#
# The scanned mailboxes are read by a human too, so IMAP \Seen can never mean
# "already handled" (AGENTS.md rule 13). Every scanner keys its persisted
# processed set on these identifiers instead.

# Identity headers fetched by the cheap pre-download peek. FROM/DATE/SUBJECT
# are needed for the fingerprint of messages that carry no Message-ID.
STATE_HEADER_SPEC = "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM DATE SUBJECT)])"


def message_state_id(msg: email.message.Message) -> str:
    """Stable identifier for a message, preferring the Message-ID header.

    Returns "" when neither a Message-ID nor any fingerprint material
    (From/Date/Subject) exists: such a message has no idempotency key, and
    callers must leave it unprocessed rather than ingest it blindly.
    """
    message_id = (msg.get("Message-ID") or "").strip()
    if message_id:
        return message_id
    parts = [
        str(msg.get("From") or ""),
        str(msg.get("Date") or ""),
        str(msg.get("Subject") or ""),
    ]
    if not any(p.strip() for p in parts):
        return ""
    fingerprint = "|".join(parts)
    return "fp:" + hashlib.sha256(fingerprint.encode("utf-8", "replace")).hexdigest()[:32]


def fetch_state_id(conn, msg_id: bytes) -> str:
    """Fetch only the identity headers so processed messages need no full download.

    Besides MESSAGE-ID this must include FROM/DATE/SUBJECT: messages without a
    Message-ID are identified by a fingerprint of those headers, and fetching
    only MESSAGE-ID used to hash every such message to the same constant —
    after the first one was processed, all the others were silently skipped
    forever. BODY.PEEK never sets \\Seen. Returns "" when no idempotency key
    can be derived.
    """
    try:
        status, data = conn.fetch(msg_id, STATE_HEADER_SPEC)
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
    return message_state_id(email.message_from_bytes(raw, policy=email.policy.default))


_FETCH_SEQ_RE = re.compile(rb"^\s*(\d+)\s+\(")


def fetch_state_ids(conn, msg_ids: list[bytes], chunk: int = 200) -> dict[bytes, str]:
    """Batched :func:`fetch_state_id`: one FETCH round trip per ``chunk`` ids.

    A mailbox scanned over a 30-day window can hold thousands of messages;
    one header round trip each made every scan take minutes. The response
    items are matched back to their sequence numbers, and any id the server
    did not answer for (or a response that cannot be parsed) falls back to
    the per-message peek, so batching can only ever save work.
    """
    wanted = [m for m in msg_ids if m]
    found: dict[bytes, str] = {}
    for start in range(0, len(wanted), chunk):
        batch = wanted[start:start + chunk]
        try:
            status, data = conn.fetch(b",".join(batch), STATE_HEADER_SPEC)
        except Exception as e:
            logger.warning(f"batched header fetch failed ({len(batch)} ids): {e}")
            status, data = "NO", []
        if status == "OK":
            requested = set(batch)
            for part in data or []:
                if not (isinstance(part, tuple) and len(part) > 1):
                    continue
                head, raw = part[0], part[1]
                match = _FETCH_SEQ_RE.match(head) if isinstance(head, (bytes, bytearray)) else None
                if not match or not isinstance(raw, (bytes, bytearray)):
                    continue
                seq = match.group(1)
                if seq in requested and seq not in found:
                    found[seq] = message_state_id(
                        email.message_from_bytes(bytes(raw), policy=email.policy.default)
                    )
        for msg_id in batch:
            if msg_id not in found:
                found[msg_id] = fetch_state_id(conn, msg_id)
    return found


def render_container_text(part: email.message.Message) -> str:
    """Render the fields of a container part (e.g. ``message/delivery-status``).

    RFC 3464 DSNs carry their machine-readable fields (``Final-Recipient``,
    ``Status``, ``Action`` ...) as *child messages* of a
    ``message/delivery-status`` part. That part is a container, so
    ``get_payload(decode=True)`` returns ``None`` and the fields are invisible
    to plain payload decoding — walking the children is the only way to read
    them.
    """
    payload = part.get_payload()
    if not isinstance(payload, list):
        return ""
    chunks = []
    for sub in payload:
        try:
            lines = [f"{k}: {v}" for k, v in sub.items()]
        except AttributeError:
            continue
        if lines:
            chunks.append("\n".join(lines))
    return "\n\n".join(chunks)


class _HTMLTextExtractor(HTMLParser):
    """Render an HTML mail part as readable plain text.

    Many mail clients (Outlook, mobile apps) send replies with an HTML part
    only — or with an empty ``text/plain`` part next to the real HTML one. A
    plain-text-only extractor recorded those genuine prospect replies with an
    empty body, so the feed showed "(Empty reply)" and sentiment classification
    had nothing to work with.
    """

    _BLOCK_TAGS = {
        "p", "div", "br", "tr", "li", "ul", "ol", "table", "blockquote",
        "pre", "h1", "h2", "h3", "h4", "h5", "h6", "header", "footer",
        "section", "article",
    }
    _SKIP_TAGS = {"script", "style", "head", "title", "meta"}
    # Void elements never receive an end tag (<meta>, <link>, <img> ...).
    # Tracking them in the skip stack would leave it unbalanced forever —
    # an Apple Mail "<head><meta …></head>" then suppressed the whole body.
    _VOID_TAGS = {"meta", "link", "br", "img", "hr", "input", "source", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._VOID_TAGS:
            if tag in self._BLOCK_TAGS:
                self._chunks.append("\n")
            return
        if tag in self._SKIP_TAGS:
            self._skip_stack.append(tag)
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._VOID_TAGS:
            return
        if tag in self._SKIP_TAGS:
            # Pop through the matching opener; tolerates stray end tags.
            if tag in self._skip_stack:
                while self._skip_stack:
                    if self._skip_stack.pop() == tag:
                        break
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._skip_stack:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks).replace("\xa0", " ")
        lines = [re.sub(r"[ \t]+", " ", line).strip()
                 for line in raw.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def html_to_text(html: str) -> str:
    """Convert an HTML mail part to plain text (stdlib only).

    Malformed markup must never kill a scan: if the parser chokes, fall back
    to a crude tag strip so the reply content is still visible.
    """
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]*>", " ", html).strip()
    return parser.text()


def _decode_payload(part: email.message.Message, payload: bytes) -> str:
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def extract_email_body(msg: email.message.EmailMessage,
                       content_types=("text/plain",)) -> str:
    """Extract the plain-text body from an email message.

    Args:
        msg: The parsed email message.
        content_types: Tuple of content types to extract. Defaults to text/plain.
            Pass ("text/plain", "message/delivery-status") for bounce DSN parsing.
            Container parts (like message/delivery-status) are expanded into
            their child fields, which is what makes DSN Status/Final-Recipient
            lines visible to the component that called us.

    When none of the requested content types yield any text (HTML-only
    replies), the ``text/html`` parts are rendered to plain text as a
    fallback so genuine replies are never stored with an empty body. A
    single-part HTML message is converted the same way instead of leaking
    raw markup into the reply feed.
    """
    body = ""
    html_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in content_types:
                payload = part.get_payload(decode=True)
                if payload:
                    body += _decode_payload(part, payload)
                    body += "\n"
                elif part.is_multipart():
                    rendered = render_container_text(part)
                    if rendered:
                        body += rendered + "\n"
            elif ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    html_parts.append(_decode_payload(part, payload))
        if not body.strip() and html_parts:
            body = html_to_text("\n".join(html_parts)) + "\n"
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = _decode_payload(msg, payload)
            if msg.get_content_type() == "text/html":
                body = html_to_text(body)
    return body


# ─── Automated-mail headers (RFC 3834 & vendor extensions) ───────────────────
#
# Body keywords are a weak signal: a human writes "I'm on vacation, but let's
# talk", a vacation responder never sets these headers by accident. Every
# scanner that has to tell a human reply apart from a machine one should ask
# the headers first.

_AUTO_REPLY_HEADERS = ("X-Autoreply", "X-Autorespond", "X-Autoresponder", "X-Mail-Autoreply")
_BULK_PRECEDENCE_RE = re.compile(r"^\s*(bulk|list|junk)\s*$", re.IGNORECASE)


def automated_header_kind(msg: email.message.Message) -> str | None:
    """Classify a message from its automation headers alone.

    Returns:
        ``'auto_replied'``   — ``Auto-Submitted: auto-replied``, an
        ``X-Autoreply``-style vendor header or ``Precedence: auto_reply``:
        a responder (vacation notice or ticket desk) answered our mail.
        ``'auto_generated'`` — ``Auto-Submitted`` with any other non-``no``
        value: a notification nobody typed.
        ``None``             — the headers say nothing; the body decides.
    """
    auto_submitted = str(msg.get("Auto-Submitted") or "").strip().lower()
    if auto_submitted and not auto_submitted.startswith("no"):
        return "auto_replied" if auto_submitted.startswith("auto-replied") else "auto_generated"
    if any(msg.get(h) for h in _AUTO_REPLY_HEADERS):
        return "auto_replied"
    if str(msg.get("Precedence") or "").strip().lower() == "auto_reply":
        return "auto_replied"
    return None


def is_bulk_mail(msg: email.message.Message) -> bool:
    """True for newsletters / mailing-list traffic that cannot be a reply.

    ``List-Unsubscribe``, ``List-Id`` or a bulk ``Precedence`` mark list
    mail; a message that additionally answers another one (``In-Reply-To`` /
    ``References``) is kept, so a ticket desk replying to our campaign with
    bulk headers still reaches the auto-reply logic.
    """
    if msg.get("In-Reply-To") or msg.get("References"):
        return False
    if msg.get("List-Unsubscribe") or msg.get("List-Id"):
        return True
    return bool(_BULK_PRECEDENCE_RE.match(str(msg.get("Precedence") or "")))


# ─── Campaign echo stripping ─────────────────────────────────────────────────
#
# Quote-header detection can never know every mail client and locale. The
# campaign we sent is the one text we know for certain, so any reply line that
# repeats it is our own copy quoted back — never the prospect's words. This is
# the backstop that keeps a footer like "reply 'remove me' to opt out" from
# reading as an opt-out when the client's quote header was not recognised.

_TEMPLATE_TAG_RE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_URL_RE = re.compile(r"https?://\S+|<[^>\s]*>")
_ECHO_SPACE_RE = re.compile(r"\s+")
_ECHO_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'",
                                   "“": '"', "”": '"'})
# A reply line shorter than this is too generic ("Thanks,") to call an echo
# by substring; whole-line equality has a lower floor.
ECHO_MIN_SUBSTRING = 20
ECHO_MIN_LINE = 8


def _echo_norm(text: str) -> str:
    text = (text or "").translate(_ECHO_APOSTROPHES)
    text = _URL_RE.sub(" ", text)
    text = text.replace("*", " ").replace("_", " ")
    return _ECHO_SPACE_RE.sub(" ", text).strip(" >\t").lower()


def campaign_echo_index(*sources: str) -> tuple[list[str], set[str]]:
    """Build the lookup used by :func:`strip_campaign_echo`.

    ``sources`` are campaign bodies (HTML or plain) and template bodies.
    Returns ``(segments, lines)``: whitespace-collapsed runs of text between
    template tags (so a re-wrapped quoted line is still a substring), and
    the exact tag-free lines.
    """
    segments: list[str] = []
    lines: set[str] = set()
    for src in sources:
        if not src or not isinstance(src, str):
            continue
        text = html_to_text(src) if "<" in src and ">" in src else src
        for chunk in _TEMPLATE_TAG_RE.split(text):
            norm = _echo_norm(chunk)
            if len(norm) >= ECHO_MIN_SUBSTRING:
                segments.append(norm)
        for line in text.splitlines():
            if _TEMPLATE_TAG_RE.search(line):
                continue
            norm = _echo_norm(line)
            if len(norm) >= ECHO_MIN_LINE:
                lines.add(norm)
    return segments, lines


def _is_long_echo(norm: str, segments: list[str]) -> bool:
    if len(norm) < ECHO_MIN_SUBSTRING:
        return False
    if any(norm in seg for seg in segments):
        return True  # our copy, possibly re-wrapped by the client
    # A personalised line ("Hi Jane, if you're interested…") is a long static
    # run of our copy once the substituted words at either end are dropped.
    # At least 60% of the line must remain, so a prospect's own sentence that
    # merely ends with a phrase of ours is kept.
    words = norm.split(" ")
    floor = max(ECHO_MIN_SUBSTRING, len(norm) * 0.6)
    for i in range(1, len(words)):
        head, tail = " ".join(words[:-i]), " ".join(words[i:])
        if len(tail) < floor and len(head) < floor:
            break
        for part in (tail, head):
            if len(part) >= floor and any(part in seg for seg in segments):
                return True
    return False


def strip_campaign_echo(reply_text: str, index: tuple[list[str], set[str]] | None) -> str:
    """Drop the reply lines that repeat the campaign copy we sent.

    A long line (``ECHO_MIN_SUBSTRING``+) that is a run of our copy is always
    dropped. A short line equal to one of ours ("Unsubscribe" — a footer link
    text) is dropped only inside a block of our quoted copy: a prospect whose
    whole answer is the single word "Unsubscribe" means it.
    """
    if not reply_text or not index or not (index[0] or index[1]):
        return reply_text
    segments, lines = index
    kept: list[str] = []
    in_echo = False
    for ln in reply_text.splitlines():
        norm = _echo_norm(ln)
        if _is_long_echo(norm, segments):
            in_echo = True
            continue
        if in_echo and len(norm) >= ECHO_MIN_LINE and norm in lines:
            continue
        kept.append(ln)
    return "\n".join(kept).strip()


async def campaign_echo_index_for(client, campaign: dict | None, cache: dict) -> tuple[list[str], set[str]] | None:
    """Echo index for a ListMonk campaign: its body, alt body and template.

    ``cache`` is per scan (keys ``("c", id)`` / ``("t", id)``) so a busy
    campaign costs at most one campaign and one template fetch. The campaign
    listing normally carries ``body`` already; it is fetched only when
    missing. Any upstream failure degrades to the parts we do have — echo
    stripping is a backstop and must never fail a scan.
    """
    if not campaign or not campaign.get("id"):
        return None
    key = ("c", campaign["id"])
    if key in cache:
        return cache[key]

    full = campaign
    if not isinstance(campaign.get("body"), str):
        try:
            res = await client.get_campaign(campaign["id"])
            data = res.get("data") if isinstance(res, dict) else None
            if isinstance(data, dict):
                full = data
        except Exception as e:
            logger.debug(f"echo index: campaign {campaign['id']} fetch failed: {e}")

    sources = [full.get("body"), full.get("altbody")]
    template_id = full.get("template_id")
    if isinstance(template_id, int) and template_id:
        tkey = ("t", template_id)
        if tkey not in cache:
            body = None
            try:
                res = await client.get_template(template_id)
                data = res.get("data") if isinstance(res, dict) else None
                body = data.get("body") if isinstance(data, dict) else None
            except Exception as e:
                logger.debug(f"echo index: template {template_id} fetch failed: {e}")
            cache[tkey] = body if isinstance(body, str) else None
        sources.append(cache[tkey])

    index = campaign_echo_index(*[s for s in sources if isinstance(s, str)])
    cache[key] = index if (index[0] or index[1]) else None
    return cache[key]
