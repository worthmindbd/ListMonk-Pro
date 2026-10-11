from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import StreamingResponse
from typing import Optional
from app.services.csv_converter import detect_columns, convert_csv
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


@router.post("/detect-columns")
async def detect_csv_columns(file: UploadFile = File(...)):
    """Upload a CSV and detect its columns + preview rows."""
    content = await _read_upload(file)
    result = detect_columns(content)
    return result


@router.post("/convert")
async def convert_csv_file(
    file: UploadFile = File(...),
    email_column: str = Form(...),
    name_column: Optional[str] = Form(None),
    attribute_columns: Optional[str] = Form(None),
):
    """Convert a CSV to ListMonk format and return as download."""
    content = await _read_upload(file)

    attrs = []
    if attribute_columns:
        try:
            attrs = json.loads(attribute_columns)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON in attribute_columns")

    result = convert_csv(content, email_column, name_column, attrs)

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
    list_ids: str = Form(...),
    mode: str = Form("subscribe"),
):
    """Convert CSV to ListMonk format and immediately import to ListMonk."""
    content = await _read_upload(file)

    attrs = []
    if attribute_columns:
        try:
            attrs = json.loads(attribute_columns)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON in attribute_columns")

    result = convert_csv(content, email_column, name_column, attrs)

    if "error" in result.get("stats", {}):
        raise HTTPException(status_code=400, detail=result["stats"]["error"])

    # Import to ListMonk
    try:
        parsed_list_ids = json.loads(list_ids)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON in list_ids")
    import_params = {
        "mode": mode,
        "delim": ",",
        "lists": parsed_list_ids,
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
