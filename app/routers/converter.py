from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse
from typing import Optional, Tuple
from app.services.csv_converter import detect_columns, convert_csv, preview_conversion
from app.services.listmonk_client import listmonk
import json
import io

router = APIRouter()

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


async def _read_upload(file: UploadFile) -> bytes:
    """Read an uploaded file, rejecting oversized payloads before buffering."""
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
        )
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file uploaded")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
        )
    return content


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
    return [int(i) for i in parsed]


@router.post("/detect-columns")
async def detect_csv_columns(file: UploadFile = File(...)):
    """Upload a CSV and detect its columns + preview rows."""
    content = await _read_upload(file)
    result = detect_columns(content)
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

    result = preview_conversion(
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

    result = convert_csv(content, email_column, name_column, attrs, auto_attributes=auto_attributes)

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
):
    """Convert CSV to ListMonk format and immediately import to ListMonk."""
    content = await _read_upload(file)
    attrs, auto_attributes = _parse_mapping(attribute_mode, attribute_columns)

    result = convert_csv(content, email_column, name_column, attrs, auto_attributes=auto_attributes)

    if "error" in result.get("stats", {}):
        raise HTTPException(status_code=400, detail=result["stats"]["error"])

    # Import to ListMonk
    import_params = {
        "mode": mode,
        "delim": ",",
        "lists": _parse_list_ids(list_ids),
        "overwrite": True,
    }
    import_result = await listmonk.import_subscribers(
        result["csv_content"].encode("utf-8"),
        "listmonk_import.csv",
        import_params,
    )
    return {
        "conversion_stats": result["stats"],
        "import_result": import_result,
    }