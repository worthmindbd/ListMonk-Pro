"""Footer unsubscribe link toggle for ListMonk campaign templates.

ListMonk's ``privacy.unsubscribe_header`` setting only controls the
``List-Unsubscribe`` *header* (the "Unsubscribe" button Gmail/Yahoo draw next
to the sender). The visible link in the message body comes from the template:
``<a href="{{ UnsubscribeURL }}">``. Toggling the header therefore never
changed the email, which is what this module fixes.

Hiding is reversible and stores nothing: the link markup is wrapped in a Go
template block that never renders::

    {{/* lmp:unsub-off */}}{{ if false }}<a href="{{ UnsubscribeURL }}">…</a>{{ end }}{{/* /lmp:unsub-off */}}

Restoring strips the wrapper and leaves the original markup byte-for-byte.
A paragraph whose only links are unsubscribe links ("If you'd prefer not to
hear from me, unsubscribe here or manage preferences.") is hidden whole, so
no dangling sentence is left behind; a paragraph that also carries other links
(ListMonk's default "Unsubscribe · View in browser" footer) only loses the
unsubscribe anchor itself.
"""

import asyncio
import logging
import re

import httpx

logger = logging.getLogger("template_unsubscribe")

# Template types that render campaign mail. Transactional templates are sent
# per-recipient by integrations and are left alone.
CAMPAIGN_TEMPLATE_TYPES = {"campaign", "campaign_visual"}

_OPEN = "{{/* lmp:unsub-off */}}{{ if false }}"
_CLOSE = "{{ end }}{{/* /lmp:unsub-off */}}"
_HIDDEN_RE = re.compile(
    r"\{\{/\* lmp:unsub-off \*/\}\}\{\{ if false \}\}(.*?)\{\{ end \}\}\{\{/\* /lmp:unsub-off \*/\}\}",
    re.DOTALL,
)

_UNSUB = "UnsubscribeURL"
# Innermost paragraph (no nested <p>) — matched lazily so it never spans two.
_PARAGRAPH_RE = re.compile(r"<p\b[^>]*>(?:(?!<p\b|</p>).)*?</p>", re.DOTALL | re.IGNORECASE)
_ANCHOR_OPEN_RE = re.compile(r"<a\b[^>]*>", re.DOTALL | re.IGNORECASE)
_UNSUB_ANCHOR_RE = re.compile(
    r"<a\b[^>]*\bhref\s*=\s*([\"'])[^\"']*UnsubscribeURL[^\"']*\1[^>]*>.*?</a>",
    re.DOTALL | re.IGNORECASE,
)
# A bare tag, e.g. a plain-text footer "Unsubscribe: {{ UnsubscribeURL }}".
_UNSUB_TAG_RE = re.compile(r"\{\{-?\s*UnsubscribeURL\s*-?\}\}")

# ListMonk restarts itself after every settings PUT, and its reverse proxy
# answers 502/503/504 for the few seconds that takes. A template call landing
# in that window is retried instead of failing the whole toggle.
TRANSIENT_STATUSES = {502, 503, 504}
RETRY_DELAYS = (1.0, 2.0, 3.0, 4.0, 5.0)


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in TRANSIENT_STATUSES
    return isinstance(exc, httpx.TransportError)


async def _retrying(call, *args):
    for delay in RETRY_DELAYS:
        try:
            return await call(*args)
        except Exception as exc:
            if not _is_transient(exc):
                raise
            logger.warning(f"ListMonk unavailable ({exc}); retrying in {delay:.0f}s")
            await asyncio.sleep(delay)
    return await call(*args)


def describe_error(exc: Exception) -> str:
    """A readable reason instead of httpx's multi-line MDN-link message."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in TRANSIENT_STATUSES:
            return f"ListMonk is unreachable or restarting (HTTP {code}). Try again in a few seconds."
        return f"ListMonk rejected the request (HTTP {code})"
    if isinstance(exc, httpx.TransportError):
        return "ListMonk could not be reached. Try again in a few seconds."
    return str(exc)


# One toggle at a time: two overlapping runs would read the same template
# bodies and the later write would silently undo the earlier one.
_TOGGLE_LOCK = asyncio.Lock()


def _wrap(markup: str) -> str:
    return f"{_OPEN}{markup}{_CLOSE}"


def _map_visible(body: str, fn) -> str:
    """Apply ``fn`` to the parts of ``body`` outside already-hidden blocks."""
    out = []
    pos = 0
    for m in _HIDDEN_RE.finditer(body):
        out.append(fn(body[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(fn(body[pos:]))
    return "".join(out)


def _hide_paragraphs(text: str) -> str:
    def repl(m: re.Match) -> str:
        block = m.group(0)
        if _UNSUB not in block:
            return block
        anchors = _ANCHOR_OPEN_RE.findall(block)
        if anchors and all(_UNSUB in a for a in anchors):
            return _wrap(block)
        return block
    return _PARAGRAPH_RE.sub(repl, text)


def _hide_anchors(text: str) -> str:
    return _UNSUB_ANCHOR_RE.sub(lambda m: _wrap(m.group(0)), text)


def _hide_tags(text: str) -> str:
    return _UNSUB_TAG_RE.sub(lambda m: _wrap(m.group(0)), text)


def hide_unsubscribe_links(body: str) -> str:
    """Wrap every visible unsubscribe link in ``body`` in a never-rendered block."""
    body = body or ""
    for step in (_hide_paragraphs, _hide_anchors, _hide_tags):
        body = _map_visible(body, step)
    return body


def show_unsubscribe_links(body: str) -> str:
    """Undo :func:`hide_unsubscribe_links` — restores the original markup."""
    return _HIDDEN_RE.sub(lambda m: m.group(1), body or "")


def unsubscribe_link_state(body: str) -> str:
    """'on', 'off', 'mixed', or 'none' (the template never had a link)."""
    body = body or ""
    hidden = bool(_HIDDEN_RE.search(body))
    visible = _UNSUB in _HIDDEN_RE.sub("", body)
    if hidden and visible:
        return "mixed"
    if hidden:
        return "off"
    if visible:
        return "on"
    return "none"


async def _campaign_templates(listmonk, retry: bool = False) -> list[dict]:
    res = await (_retrying(listmonk.get_templates) if retry else listmonk.get_templates())
    data = res.get("data") if isinstance(res, dict) else None
    if not isinstance(data, list):
        raise ValueError("Unexpected templates response from ListMonk")
    return [t for t in data if isinstance(t, dict) and t.get("type") in CAMPAIGN_TEMPLATE_TYPES]


def _summarize(templates: list[dict]) -> dict:
    rows = [{
        "id": t.get("id"),
        "name": t.get("name") or f"Template {t.get('id')}",
        "type": t.get("type"),
        "is_default": bool(t.get("is_default")),
        "state": unsubscribe_link_state(t.get("body") or ""),
    } for t in templates]
    counts = {s: sum(1 for r in rows if r["state"] == s) for s in ("on", "off", "mixed", "none")}
    if counts["mixed"] or (counts["on"] and counts["off"]):
        overall = "mixed"
    elif counts["off"]:
        overall = "off"
    elif counts["on"]:
        overall = "on"
    else:
        overall = "none"
    return {"state": overall, "counts": counts, "templates": rows}


async def get_unsubscribe_link_status(listmonk) -> dict:
    """Per-template footer link state across all campaign templates."""
    return _summarize(await _campaign_templates(listmonk))


async def set_unsubscribe_links(listmonk, enabled: bool, template_ids: list[int] | None = None) -> dict:
    """Show (``enabled=True``) or hide the footer unsubscribe link.

    Only templates that change are written. Templates without any unsubscribe
    link are reported in ``without_link`` rather than silently counted as done.
    Partial failures are returned (``errors``/``failed``), never swallowed.
    """
    async with _TOGGLE_LOCK:
        templates = await _campaign_templates(listmonk, retry=True)
        if template_ids:
            wanted = set(template_ids)
            templates = [t for t in templates if t.get("id") in wanted]

        transform = show_unsubscribe_links if enabled else hide_unsubscribe_links
        updated, unchanged, without_link, failed = [], [], [], []
        for tpl in templates:
            body = tpl.get("body") or ""
            if unsubscribe_link_state(body) == "none":
                without_link.append({"id": tpl.get("id"), "name": tpl.get("name")})
                continue
            new_body = transform(body)
            if new_body == body:
                unchanged.append(tpl.get("id"))
                continue
            payload = {
                "name": tpl.get("name"),
                "type": tpl.get("type"),
                "subject": tpl.get("subject") or "",
                "body": new_body,
            }
            # The visual editor's block JSON must round-trip or ListMonk
            # rejects (or wipes) the visual template.
            if tpl.get("body_source") is not None:
                payload["body_source"] = tpl.get("body_source")
            try:
                await _retrying(listmonk.update_template, tpl["id"], payload)
                updated.append(tpl.get("id"))
            except Exception as exc:
                logger.error(f"Failed to update template {tpl.get('id')}: {exc}")
                failed.append({"id": tpl.get("id"), "name": tpl.get("name"), "error": describe_error(exc)})

        logger.info(
            f"Footer unsubscribe link {'enabled' if enabled else 'disabled'}: "
            f"{len(updated)} updated, {len(unchanged)} unchanged, {len(failed)} failed"
        )
        return {
            "enabled": enabled,
            "updated": updated,
            "unchanged": unchanged,
            "without_link": without_link,
            "errors": len(failed),
            "failed": failed,
        }
