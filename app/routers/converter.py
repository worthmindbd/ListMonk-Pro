from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse
from typing import Optional, Tuple
from app.services.csv_converter import detect_columns, convert_csv, preview_conversion
from app.services.listmonk_client import listmonk
import asyncio
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
    overwrite: bool,
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

    ow_user = overwrite if overwrite_userinfo is None else overwrite_userinfo
    ow_status = overwrite if overwrite_subscription_status is None else overwrite_subscription_status

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
            "X-Conversion-Stats": json.dumps(result["stats"]),
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
    overwrite: bool = Form(True),
    overwrite_userinfo: Optional[bool] = Form(None),
    overwrite_subscription_status: Optional[bool] = Form(None),
):
    """Convert CSV to ListMonk format and immediately import to ListMonk.

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

    # Import to ListMonk
    import_result = await listmonk.import_subscribers(
        result["csv_content"].encode("utf-8"),
        "listmonk_import.csv",
        import_params,
    )
    return {
        "conversion_stats": stats,
        "import_params": import_params,
        "import_result": import_result,
    }