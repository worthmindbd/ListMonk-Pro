"""Per-campaign email format switch: HTML design ↔ plain text.

"HTML design" is a branded template (logo, layout tables, CSS). "Plain text"
is what a hand-typed email looks like: one ``<div dir="ltr">`` of text and
links, no images or layout, plus a text-only MIME part (the campaign's
``altbody``). Both formats stay ``richtext`` campaigns, so the HTML is still
escaped by ListMonk's html/template and nothing about list targeting changes.

Switching only swaps the campaign's ``template_id`` and ``altbody``. The
setup a campaign had in each format is remembered in the campaign's own
``attribs`` (``lmp_email_format``), which live in ListMonk and are therefore
shared by every ListMonk Pro instance pointed at it. With no memory, the plain
version is an app-managed "twin" generated from the HTML template (marked
``{{/* lmp:plain-of=<id> */}}``), and switching back to HTML follows that
marker to the template it came from.

Nothing here compiles Go templates, so every switch is verified by rendering
the campaign preview (ListMonk compiles subject, template and altbody there);
a compile error restores the previous setup and is reported.
"""

import asyncio
import html
import logging
import re

logger = logging.getLogger("email_format")

FORMATS = ("html", "plain")
ATTRIB_KEY = "lmp_email_format"

# ListMonk refuses edits to running/finished campaigns (canEditCampaign).
EDITABLE_STATUSES = {"draft", "scheduled", "paused"}
LISTED_STATUSES = EDITABLE_STATUSES | {"running"}
# Visual campaigns carry their own layout and ignore template_id; plain ones
# are already text-only.
SWITCHABLE_CONTENT = {"richtext", "html", "markdown"}

TEMPLATE_TYPE = "campaign"
CONTENT_TAG = '{{ template "content" . }}'
TWIN_SUFFIX = " · plain"

_TWIN_MARK_RE = re.compile(r"\{\{/\* lmp:plain-of=(\d+) \*/\}\}")
# A template with images, layout tables or a stylesheet is a design.
_DESIGN_RE = re.compile(r"<(?:img|table|style)\b", re.IGNORECASE)

# ── Template conversion ─────────────────────────────────────────────────────
# Go template actions are swapped for NUL-delimited placeholders before any
# HTML is touched, so tag stripping can never cut through `{{ ... }}`.
_ACTION_RE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_PH_RE = re.compile(r"\x00(\d+)\x00")
_PARTS_RE = re.compile(r"^\{\{(-?)\s*(.*?)\s*(-?)\}\}$", re.DOTALL)
_CONTROL_WORDS = {"if", "else", "end", "with", "range", "define", "block", "break", "continue"}
_FIELD_RE = re.compile(r"^\.Subscriber\.Attribs(?:\.[A-Za-z0-9_]+)+$")
_CONTENT_RE = re.compile(r'^template\s+"content"\s+\.$')
_ASSIGN_RE = re.compile(r"^\$\w*\s*:?=")
# ListMonk's own link helpers only work in the exact shorthand its pre-compile
# regex rewrites (`{{ UnsubscribeURL }}` → `{{ UnsubscribeURL . }}`), so they
# are never wrapped or rewritten. Their output is a URL: escaping is moot.
_LISTMONK_FUNC_RE = re.compile(r"^(?:UnsubscribeURL|ManageURL|OptinURL|MessageURL|ArchiveURL|TrackLink)\b")
_TRACKVIEW_RE = re.compile(r"^TrackView(?:\s+\.)?$")

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_DOCTYPE_RE = re.compile(r"<!doctype[^>]*>", re.IGNORECASE)
_DROP_BLOCK_RE = re.compile(r"<(head|style|script|title)\b[^>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLOCK_RE = re.compile(
    r"</?(?:p|div|table|tbody|thead|tr|td|th|h[1-6]|ul|ol|li|blockquote|center|section|article|header|footer)\b[^>]*>",
    re.IGNORECASE,
)
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a\s*>", re.DOTALL | re.IGNORECASE)
_HREF_RE = re.compile(r"""\bhref\s*=\s*(["'])(.*?)\1""", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_KEPT_ANCHOR_RE = re.compile(r'<a href="[^"]*">')

# Placeholder-safe markers for the anchors kept in the plain HTML.
_A_OPEN, _A_MID, _A_CLOSE, _A_END = "\x01a\x01", "\x01m\x01", "\x01c\x01", "\x01e\x01"


def classify_template(body: str | None) -> str:
    return "html" if _DESIGN_RE.search(body or "") else "plain"


def _action_kind(action: str) -> tuple[str, str, str, str]:
    """('comment'|'control'|'content'|'trackview'|'listmonk'|'field'|'output', inner, l, r)."""
    m = _PARTS_RE.match(action)
    if not m:
        return "control", action, "", ""
    ltrim, inner, rtrim = m.group(1), m.group(2), m.group(3)
    if inner.startswith("/*"):
        kind = "comment"
    elif _CONTENT_RE.match(inner):
        kind = "content"
    elif inner.split(None, 1)[0] in _CONTROL_WORDS or inner.startswith("template ") or _ASSIGN_RE.match(inner):
        kind = "control"
    elif _TRACKVIEW_RE.match(inner):
        kind = "trackview"
    elif _LISTMONK_FUNC_RE.match(inner):
        kind = "listmonk"
    elif _FIELD_RE.match(inner):
        kind = "field"
    else:
        kind = "output"
    return kind, inner, ltrim, rtrim


def _protect(src: str) -> tuple[str, list[str]]:
    actions: list[str] = []

    def keep(m: re.Match) -> str:
        actions.append(m.group(0))
        return f"\x00{len(actions) - 1}\x00"

    return _ACTION_RE.sub(keep, src or ""), actions


def _html_to_lines(protected: str) -> list[str]:
    """Visible text of a (protected) HTML document, one entry per line.

    Anchors survive as marker sequences so the caller decides how to render
    them; every other tag is dropped. Whitespace follows HTML rules: source
    newlines are spaces, only <br> and block boundaries break lines.
    """
    s = _COMMENT_RE.sub("", protected)
    s = _DOCTYPE_RE.sub("", s)
    s = _DROP_BLOCK_RE.sub("", s)
    s = re.sub(r"<img\b[^>]*>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s)
    # Adjacent actions separated only by indentation must not print a space.
    s = re.sub(r"(\x00\d+\x00) (?=\x00)", r"\1", s)

    def anchor(m: re.Match) -> str:
        href = _HREF_RE.search(m.group(1))
        label = _TAG_RE.sub("", m.group(2)).strip()
        if not href or not label:
            return label
        return f"{_A_OPEN}{href.group(2)}{_A_MID}{label}{_A_CLOSE}"

    s = _ANCHOR_RE.sub(anchor, s)
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_RE.sub("\n\n", s)
    s = _TAG_RE.sub("", s)
    s = s.replace("&nbsp;", " ").replace("\u00a0", " ")

    lines: list[str] = []
    for raw in s.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line and (not lines or not lines[-1]):
            continue
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _is_silent(line: str, actions: list[str]) -> bool:
    """A line holding only control/comment actions (or the dropped tracking
    pixel) prints nothing, so it must not add a line break either."""
    if not line:
        return False
    rest = _PH_RE.sub(
        lambda m: "" if _action_kind(actions[int(m.group(1))])[0] in ("comment", "control", "trackview") else "x",
        line,
    )
    return not rest.strip()


def _assemble(lines: list[str], actions: list[str], eol: str) -> str:
    # Blank lines after the last visible line (e.g. a footer gutter holding
    # only the tracking pixel) would end the email in empty space.
    last = max((i for i, ln in enumerate(lines) if ln and not _is_silent(ln, actions)), default=-1)
    lines = [ln for i, ln in enumerate(lines) if i <= last or ln]
    out, pending = [], ""
    for line in lines:
        if _is_silent(line, actions):
            pending += line
            continue
        out.append(pending + line + eol)
        pending = ""
    return "".join(out) + pending


def _restore(text: str, actions: list[str], render) -> str:
    return _PH_RE.sub(lambda m: render(int(m.group(1)), actions[int(m.group(1))]), text)


def derive_plain_html(html_body: str, source_id: int) -> str:
    """Plain twin of an HTML design template (text + links, no layout)."""
    protected, actions = _protect(html_body)
    lines = _html_to_lines(protected)
    body = _assemble(lines, actions, "<br>\n")
    body = (body.replace(_A_OPEN, '<a href="').replace(_A_MID, '">')
                .replace(_A_CLOSE, "</a>"))

    # Placeholders inside a kept href are attribute context: never rewrite them.
    in_attr = {int(i) for span in _KEPT_ANCHOR_RE.findall(body) for i in _PH_RE.findall(span)}

    def render(idx: int, action: str) -> str:
        kind, inner, lt, rt = _action_kind(action)
        if kind == "trackview":
            return ""
        if kind == "content":
            return CONTENT_TAG
        if kind == "field" and idx not in in_attr:
            # The design relied on CSS `white-space: pre-line` for attribute
            # copy with newlines; plain HTML needs explicit line breaks.
            return (f"{{{{{lt} with {inner} }}}}{{{{ range $i, $l := splitList \"\\n\" (toString .) }}}}"
                    f"{{{{ if $i }}}}<br>\n{{{{ end }}}}{{{{ trim $l }}}}{{{{ end }}}}{{{{ end {rt}}}}}")
        return action

    body = _restore(body, actions, render)
    return (
        "<!doctype html>\n<html>\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n</head>\n<body>\n"
        f"{{{{/* lmp:plain-of={source_id} */}}}}<div dir=\"ltr\">\n{body}</div>\n</body>\n</html>\n"
    )


def derive_text(template_body: str, campaign_body: str = "", content_type: str = "richtext") -> str:
    """Text-only MIME part (``altbody``) for a template.

    ListMonk compiles the altbody with html/template as a standalone template,
    so output actions are wrapped in ``Safe`` (an apostrophe must not become
    ``&#39;`` in a text part) and ``{{ template "content" . }}`` — which would
    recurse into the altbody itself — is replaced by a text snapshot of the
    campaign body.
    """
    protected, actions = _protect(template_body)
    lines = _html_to_lines(protected)
    text = _assemble(lines, actions, "\n")

    def anchor(m: re.Match) -> str:
        href, label = m.group(1), m.group(2)
        return href if label == href else f"{label} ({href})"

    text = re.sub(re.escape(_A_OPEN) + "(.*?)" + re.escape(_A_MID) + "(.*?)" + re.escape(_A_CLOSE),
                  anchor, text, flags=re.DOTALL)
    text = html.unescape(text)

    def render(_idx: int, action: str) -> str:
        kind, inner, lt, rt = _action_kind(action)
        if kind == "trackview":
            return ""
        if kind == "content":
            return _content_snapshot(campaign_body, content_type)
        if kind == "field":
            return f"{{{{{lt} with {inner} }}}}{{{{ Safe (toString .) }}}}{{{{ end {rt}}}}}"
        if kind == "output":
            return f"{{{{{lt} Safe (toString ({inner})) {rt}}}}}"
        return action

    return _restore(text, actions, render).strip() + "\n"


def _content_snapshot(body: str, content_type: str) -> str:
    if not (body or "").strip():
        return ""
    if content_type in ("markdown", "plain"):
        return body.strip()
    # A content body never contains the content tag itself.
    return derive_text(body).strip()


# ── Campaign switching ──────────────────────────────────────────────────────

class FormatError(Exception):
    """A switch the campaign cannot take; ``status`` is the HTTP code to use."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


_FORMAT_LOCK = asyncio.Lock()


def _campaign_templates(res) -> dict[int, dict]:
    data = res.get("data") if isinstance(res, dict) else None
    if not isinstance(data, list):
        raise ValueError("Unexpected templates response from ListMonk")
    return {t["id"]: t for t in data if isinstance(t, dict) and t.get("type") == TEMPLATE_TYPE}


def _blocked_reason(camp: dict) -> str:
    if camp.get("content_type") == "visual":
        return "Visual campaigns carry their own layout."
    if camp.get("content_type") == "plain":
        return "Already a text-only campaign."
    if camp.get("status") == "running":
        return "Pause the campaign to switch."
    if camp.get("status") not in EDITABLE_STATUSES:
        return f"A {camp.get('status')} campaign cannot be edited."
    return ""


def _current_format(camp: dict, templates: dict[int, dict]) -> str:
    if camp.get("content_type") == "plain":
        return "text"
    if camp.get("content_type") == "visual":
        return "visual"
    tpl = templates.get(camp.get("template_id"))
    return classify_template(tpl.get("body")) if tpl else "html"


async def list_email_formats(listmonk) -> list[dict]:
    """Every campaign that can still send, with its current format."""
    templates = _campaign_templates(await listmonk.get_templates())
    res = await listmonk.get_campaigns(page=1, per_page=200)
    camps = ((res or {}).get("data") or {}).get("results") or []
    rows = []
    for c in camps:
        if c.get("status") not in LISTED_STATUSES:
            continue
        tpl = templates.get(c.get("template_id"))
        reason = _blocked_reason(c)
        rows.append({
            "id": c.get("id"),
            "name": c.get("name"),
            "status": c.get("status"),
            "content_type": c.get("content_type"),
            "template_id": c.get("template_id"),
            "template_name": tpl.get("name") if tpl else None,
            "format": _current_format(c, templates),
            "can_switch": not reason,
            "reason": reason,
        })
    return rows


def _html_fallback(cur_tpl: dict | None, templates: dict[int, dict]) -> dict:
    if cur_tpl:
        mark = _TWIN_MARK_RE.search(cur_tpl.get("body") or "")
        if mark and int(mark.group(1)) in templates:
            return templates[int(mark.group(1))]
    designs = [t for t in templates.values() if classify_template(t.get("body")) == "html"]
    default = next((t for t in designs if t.get("is_default")), None)
    if default or designs:
        return default or designs[0]
    raise FormatError(409, "No HTML design template exists to switch to.")


async def _ensure_twin(listmonk, html_tpl: dict, templates: dict[int, dict]) -> dict:
    """Create — or regenerate — the app-managed plain twin of ``html_tpl``."""
    body = derive_plain_html(html_tpl.get("body") or "", html_tpl["id"])
    marker = f"{{{{/* lmp:plain-of={html_tpl['id']} */}}}}"
    payload = {"name": f"{html_tpl.get('name')}{TWIN_SUFFIX}", "type": TEMPLATE_TYPE, "subject": "", "body": body}
    existing = next((t for t in templates.values() if marker in (t.get("body") or "")), None)
    if existing:
        if existing.get("body") != body:
            await listmonk.update_template(existing["id"], payload)
        return {**existing, **payload}
    created = (await listmonk.create_template(payload)).get("data") or {}
    return {**payload, **created}


def _text_source(tpl: dict, templates: dict[int, dict]) -> str:
    """Template body to derive a text part from: a twin's HTML source when it
    still exists (its attribute copy converts to clean `Safe` output), else
    the template itself."""
    mark = _TWIN_MARK_RE.search(tpl.get("body") or "")
    if mark and int(mark.group(1)) in templates:
        return templates[int(mark.group(1))].get("body") or ""
    return tpl.get("body") or ""


def _update_payload(camp: dict, template_id, altbody, attribs: dict) -> dict:
    # Lists and media must be resent or ListMonk drops them; altbody "" is
    # stored as NULL by ListMonk.
    return {
        "template_id": template_id,
        "altbody": altbody or "",
        "attribs": attribs,
        "lists": [int(l["id"] if isinstance(l, dict) else l) for l in camp.get("lists") or []],
        "media": [int(m["id"] if isinstance(m, dict) else m) for m in camp.get("media") or []],
    }


async def set_email_format(listmonk, campaign_id: int, fmt: str) -> dict:
    if fmt not in FORMATS:
        raise FormatError(400, "format must be 'html' or 'plain'")

    async with _FORMAT_LOCK:
        camp = (await listmonk.get_campaign(campaign_id)).get("data") or {}
        if not camp:
            raise FormatError(404, "Campaign not found")
        reason = _blocked_reason(camp)
        if reason:
            raise FormatError(409, reason)

        templates = _campaign_templates(await listmonk.get_templates())
        cur_id = camp.get("template_id")
        cur_tpl = templates.get(cur_id)
        current = _current_format(camp, templates)
        if current == fmt:
            return {"id": campaign_id, "format": fmt, "template_id": cur_id,
                    "template_name": cur_tpl.get("name") if cur_tpl else None, "changed": False}

        attribs = dict(camp.get("attribs") or {})
        memory = dict(attribs.get(ATTRIB_KEY) or {})
        memory[current] = {"template_id": cur_id, "altbody": camp.get("altbody")}

        remembered = memory.get(fmt) or {}
        target = templates.get(remembered.get("template_id"))
        if target and classify_template(target.get("body")) == fmt:
            altbody = remembered.get("altbody")
            if fmt == "plain" and not altbody:
                altbody = derive_text(_text_source(target, templates), camp.get("body") or "",
                                      camp.get("content_type"))
        elif fmt == "plain":
            html_tpl = cur_tpl or _html_fallback(None, templates)
            target = await _ensure_twin(listmonk, html_tpl, templates)
            altbody = derive_text(html_tpl.get("body") or "", camp.get("body") or "", camp.get("content_type"))
        else:
            target = _html_fallback(cur_tpl, templates)
            # A text part helps an HTML email too; keep the one it has.
            altbody = camp.get("altbody")

        memory[fmt] = {"template_id": target["id"], "altbody": altbody}
        attribs[ATTRIB_KEY] = memory
        await listmonk.update_campaign(campaign_id, _update_payload(camp, target["id"], altbody, attribs))

        # ListMonk only compiles templates when a campaign starts; the preview
        # compiles subject, template and altbody now. Roll back on failure so
        # a broken template can never reach a send.
        try:
            await listmonk.preview_campaign(campaign_id)
        except Exception as exc:
            logger.error(f"Format switch of campaign {campaign_id} failed to compile: {exc}")
            await listmonk.update_campaign(
                campaign_id, _update_payload(camp, cur_id, camp.get("altbody"), dict(camp.get("attribs") or {})))
            raise FormatError(422, "ListMonk could not compile the switched template, so the campaign "
                                   "was left unchanged.")

        logger.info(f"Campaign {campaign_id} switched to {fmt} (template {target['id']})")
        return {"id": campaign_id, "format": fmt, "template_id": target["id"],
                "template_name": target.get("name"), "changed": True}
