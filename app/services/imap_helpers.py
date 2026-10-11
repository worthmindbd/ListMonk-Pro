"""Shared IMAP helpers: email validation, IMAP date formatting, body extraction."""

import email
import re
from datetime import datetime

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
    """
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct in content_types:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        body += payload.decode(charset, errors="replace")
                    except (LookupError, UnicodeDecodeError):
                        body += payload.decode("utf-8", errors="replace")
                    body += "\n"
                elif part.is_multipart():
                    rendered = render_container_text(part)
                    if rendered:
                        body += rendered + "\n"
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                body = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                body = payload.decode("utf-8", errors="replace")
    return body
