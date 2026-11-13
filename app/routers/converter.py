from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse
from typing import Optional, Tuple
from app.services.csv_converter import detect_columns, convert_csv, preview_conversion
from app.services.listmonk_client import listmonk
from app.services import blocklist_registry, email_validation
import asyncio
import csv
import json
import io

router = APIRouter()

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


async def _read_upload(file: UploadFile) -> bytes:
    """Read an uploaded file, rejecting oversized payloads before buffering.

    The size is enforced incrementally: a client that does not declare
    `file.size` (chunked multipart) must not be able to make the worker
    allocate the whole upload before the 413 fires.
    """
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
        )
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
            )
        chunks.append(chunk)
    if not total:
        raise HTTPException(status_code=400, detail="Empty file uploaded")
    return b"".join(chunks)


def _parse_mapping(
    attribute_mode: str,
    attribute_columns: Optional[str],
) -> Tuple[list, bool]:
    """Turn the form's attribute mapping into ``(columns, auto_attributes)``.

    ``auto`` keeps every unmapped column (the default and the safe choice - no
    source column is silently dropped). ``selected`` converts only the checked
    columns. A request with explicit ``attribute_columns`` but no mode is
    treated as ``selected`` for backwards compatibility.
    """
    mode = (attribute_mode or "").strip().lower()
    if not mode:
        mode = "selected" if attribute_columns is not None else "auto"
    if mode not in ("auto", "selected"):
        raise HTTPException(status_code=400, detail="Invalid attribute_mode (use 'auto' or 'selected')")

    attrs: list = []
    if attribute_columns:
        try:
            attrs = json.loads(attribute_columns)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON in attribute_columns")
        if not isinstance(attrs, list):
            raise HTTPException(status_code=400, detail="attribute_columns must be a JSON array")

    return attrs, mode == "auto"


def _parse_list_ids(list_ids: str) -> list:
    """Parse the JSON list_ids form field into a list of integers."""
    try:
        parsed = json.loads(list_ids)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON in list_ids")
    if not isinstance(parsed, list):
        raise HTTPException(status_code=400, detail="list_ids must be a JSON array")
    try:
        return [int(i) for i in parsed]
    except (TypeError, ValueError):
        # A 500 here would hide a plain client error (["abc"], [{"id":1}]).
        raise HTTPException(status_code=400, detail="list_ids must contain only numbers")


# ListMonk's importer accepts exactly these subscription statuses; anything
# else is rejected before the upload is even converted.
VALID_SUB_STATUSES = ("confirmed", "unconfirmed", "unsubscribed")
VALID_IMPORT_MODES = ("subscribe", "blocklist")


def _parse_import_options(
    mode: str,
    subscription_status: Optional[str],
    overwrite: Optional[bool],
    overwrite_userinfo: Optional[bool],
    overwrite_subscription_status: Optional[bool],
) -> dict:
    """Build ListMonk's import ``params`` payload.

    ListMonk >= 4.1 understands ``overwrite_userinfo`` /
    ``overwrite_subscription_status`` separately, but a legacy ``overwrite:
    true`` force-enables both, so the legacy flag is only sent when the
    granular intent matches it — older ListMonk versions then still behave as
    before while new ones honour the individual switches.

    ``subscription_status`` defaults to ``confirmed`` for subscribe imports:
    a double-opt-in list only mails confirmed subscriptions, and silently
    importing a whole lead list as ``unconfirmed`` (ListMonk's own default)
    leaves it unmailable with no visible error.
    """
    mode = (mode or "").strip().lower()
    if mode not in VALID_IMPORT_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid import mode '{mode}' (use 'subscribe' or 'blocklist')")

    status = (subscription_status or "").strip().lower() or None
    if status and status not in VALID_SUB_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid subscription_status '{status}' (use one of {', '.join(VALID_SUB_STATUSES)})",
        )
    if not status:
        status = "confirmed" if mode == "subscribe" else None

    # Defaults when the caller says nothing: refresh names/attributes, but
    # never touch subscription statuses. Overwriting statuses re-subscribes
    # everyone who opted out of the target lists, so it has to be asked for
    # (the UI always sends both switches explicitly).
    ow_user = overwrite_userinfo if overwrite_userinfo is not None else (
        True if overwrite is None else overwrite)
    ow_status = overwrite_subscription_status if overwrite_subscription_status is not None else (
        False if overwrite is None else overwrite)

    params = {
        "mode": mode,
        "delim": ",",
        "overwrite_userinfo": bool(ow_user),
        "overwrite_subscription_status": bool(ow_status),
        "overwrite": bool(ow_user and ow_status),
    }
    if status:
        params["subscription_status"] = status
    return params


# Response headers share one small budget with everything a reverse proxy
# adds (nginx's default proxy_buffer_size is 4 KB).
_STATS_HEADER_MAX = 3000


def _stats_header(stats: dict) -> str:
    """Serialise conversion stats for the X-Conversion-Stats header.

    The stats carry per-column lists (one entry per attribute column), and a
    wide lead export (100+ columns) pushed the header past common proxy
    limits — nginx then answers 502 "upstream sent too big header" and the
    converted file never arrives. Past the budget only the scalar counters
    are kept, which is all the UI's summary toast reads.
    """
    full = json.dumps(stats)
    if len(full) <= _STATS_HEADER_MAX:
        return full
    return json.dumps({
        **{k: v for k, v in stats.items() if not isinstance(v, (list, dict))},
        "truncated": True,
    })


@router.post("/detect-columns")
async def detect_csv_columns(file: UploadFile = File(...)):
    """Upload a CSV and detect its columns + preview rows."""
    content = await _read_upload(file)
    # Detection decodes the whole upload several times; keep that off the
    # event loop so a big file cannot freeze every other request/worker.
    result = await asyncio.to_thread(detect_columns, content)
    return result


@router.post("/preview")
async def preview_csv_file(
    file: UploadFile = File(...),
    email_column: str = Form(...),
    name_column: Optional[str] = Form(None),
    attribute_columns: Optional[str] = Form(None),
    attribute_mode: Optional[str] = Form(None),
    limit: int = Form(10),
):
    """Convert a CSV and return the first rows as JSON for the preview table."""
    content = await _read_upload(file)
    attrs, auto_attributes = _parse_mapping(attribute_mode, attribute_columns)

    # Multi-pass CSV conversion is CPU/decode-bound; run it in a worker thread.
    result = await asyncio.to_thread(
        preview_conversion,
        content,
        email_column,
        name_column,
        attrs,
        auto_attributes=auto_attributes,
        limit=min(max(limit, 1), 100),
    )
    if "error" in result["stats"]:
        raise HTTPException(status_code=400, detail=result["stats"]["error"])
    return result


@router.post("/convert")
async def convert_csv_file(
    file: UploadFile = File(...),
    email_column: str = Form(...),
    name_column: Optional[str] = Form(None),
    attribute_columns: Optional[str] = Form(None),
    attribute_mode: Optional[str] = Form(None),
):
    """Convert a CSV to ListMonk format and return as download."""
    content = await _read_upload(file)
    attrs, auto_attributes = _parse_mapping(attribute_mode, attribute_columns)

    # Multi-pass CSV conversion is CPU/decode-bound; run it in a worker thread.
    result = await asyncio.to_thread(
        convert_csv, content, email_column, name_column, attrs,
        auto_attributes=auto_attributes,
    )

    if "error" in result.get("stats", {}):
        raise HTTPException(status_code=400, detail=result["stats"]["error"])

    return StreamingResponse(
        io.StringIO(result["csv_content"]),
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=listmonk_import.csv",
            "X-Conversion-Stats": _stats_header(result["stats"]),
        },
    )


@router.post("/convert-and-import")
async def convert_and_import(
    file: UploadFile = File(...),
    email_column: str = Form(...),
    name_column: Optional[str] = Form(None),
    attribute_columns: Optional[str] = Form(None),
    attribute_mode: Optional[str] = Form(None),
    list_ids: str = Form(...),
    mode: str = Form("subscribe"),
    subscription_status: Optional[str] = Form(None),
    overwrite: Optional[bool] = Form(None),
    overwrite_userinfo: Optional[bool] = Form(None),
    overwrite_subscription_status: Optional[bool] = Form(None),
    validate_emails: Optional[bool] = Form(False),
):
    """Convert CSV to ListMonk format and immediately import to ListMonk.

    With ``validate_emails`` every address is checked first (format, domain,
    mail server) and the rows with an ``invalid`` verdict are left out of the
    import. Only definitive answers drop a row: risky addresses and DNS
    failures (``unknown``) are imported and merely counted.

    ``subscription_status`` controls how new subscriptions are created on
    double-opt-in lists (``confirmed`` by default so an imported lead list is
    actually mailable); the two ``overwrite_*`` switches map onto ListMonk's
    granular upsert flags.
    """
    content = await _read_upload(file)
    attrs, auto_attributes = _parse_mapping(attribute_mode, attribute_columns)

    # Multi-pass CSV conversion is CPU/decode-bound; run it in a worker thread.
    result = await asyncio.to_thread(
        convert_csv, content, email_column, name_column, attrs,
        auto_attributes=auto_attributes,
    )

    if "error" in result.get("stats", {}):
        raise HTTPException(status_code=400, detail=result["stats"]["error"])

    stats = result.get("stats", {})
    if not stats.get("converted"):
        raise HTTPException(
            status_code=400,
            detail="Nothing to import: every row was skipped (missing or malformed e-mail addresses).",
        )

    import_params = _parse_import_options(
        mode, subscription_status, overwrite, overwrite_userinfo, overwrite_subscription_status,
    )
    import_params["lists"] = _parse_list_ids(list_ids)
    if not import_params["lists"]:
        raise HTTPException(status_code=400, detail="Select at least one list to import into")

    csv_content = result["csv_content"]
    validation = None
    if validate_emails:
        csv_content, validation = await _drop_invalid_rows(csv_content)
        if not validation["kept"]:
            raise HTTPException(
                status_code=400,
                detail=f"Nothing to import: all {validation['removed']} address(es) failed validation.",
            )

    # Import to ListMonk
    import_result = await listmonk.import_subscribers(
        csv_content.encode("utf-8"),
        "listmonk_import.csv",
        import_params,
    )
    if import_params["mode"] == "blocklist":
        # A deliberate blocklisting: auto-unblock must never reverse it. The
        # import runs in ListMonk's background, so no ids exist yet — the
        # addresses are recorded instead.
        blocklist_registry.mark_emails(_csv_emails(csv_content))
    return {
        "conversion_stats": stats,
        "validation": validation,
        "import_params": import_params,
        "import_result": import_result,
    }


def _csv_emails(csv_content: str) -> list[str]:
    """The ``email`` column of a converted CSV."""
    rows = list(csv.reader(io.StringIO(csv_content)))
    if not rows:
        return []
    try:
        col = [h.strip().lower() for h in rows[0]].index("email")
    except ValueError:
        col = 0
    return [r[col] for r in rows[1:] if len(r) > col and r[col]]


# Addresses listed back to the UI when rows are dropped (the rest are counted).
VALIDATION_SAMPLE = 50


async def _drop_invalid_rows(csv_content: str) -> tuple[str, dict]:
    """Validate the converted CSV's addresses and remove the invalid rows.

    Returns the filtered CSV and ``{checked, kept, removed, risky, unknown,
    removed_sample: [{email, reason}]}``. The DNS-dependent checks go through
    the shared validator (per-domain cache, bounded concurrency).
    """
    rows = list(csv.reader(io.StringIO(csv_content)))
    if not rows:
        return csv_content, {"checked": 0, "kept": 0, "removed": 0, "risky": 0, "unknown": 0, "removed_sample": []}
    header, body = rows[0], rows[1:]
    try:
        col = [h.strip().lower() for h in header].index("email")
    except ValueError:
        col = 0
    emails = sorted({r[col] for r in body if len(r) > col and r[col]})
    verdicts = {v["email"]: v for v in await email_validation.validate_many(emails)}
    kept, removed = [], []
    risky = unknown = 0
    for r in body:
        v = verdicts.get(email_validation.normalise(r[col]) if len(r) > col else "")
        verdict = v["verdict"] if v else "valid"
        if verdict == "invalid":
            removed.append({"email": r[col], "reason": v.get("detail") or ", ".join(v.get("reasons") or [])})
            continue
        if verdict == "risky":
            risky += 1
        elif verdict == "unknown":
            unknown += 1
        kept.append(r)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(kept)
    return out.getvalue(), {
        "checked": len(body),
        "kept": len(kept),
        "removed": len(removed),
        "risky": risky,
        "unknown": unknown,
        "removed_sample": removed[:VALIDATION_SAMPLE],
    }

