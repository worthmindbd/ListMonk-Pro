"""Shared IMAP helpers: email validation, IMAP date formatting, body extraction."""

import email
import re
from datetime import datetime
from html.parser import HTMLParser

_ENG_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w.-]+\.\w+$")


def safe_email_for_query(email_addr: str) -> str | None:
    """Validate email format and escape for SQL query interpolation."""
    if not _EMAIL_RE.match(email_addr):
        return None
    return email_addr.replace("'", "''")


def imap_date(dt: datetime) -> str:
    """Format a datetime as an IMAP SINCE date with English month names."""
    return f"{dt.day:02d}-{_ENG_MONTHS[dt.month - 1]}-{dt.year}"


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

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
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
