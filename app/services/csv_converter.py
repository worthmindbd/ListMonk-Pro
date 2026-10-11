"""
CSV to ListMonk format converter.
Refactored from the standalone convert.py script.

ListMonk requires subscribers CSV with columns: email, name, attributes
where attributes is a JSON string of extra fields.
"""

import csv
import io
import json
from typing import Optional


ENCODINGS = ["utf-8-sig", "utf-8", "latin-1", "cp1252"]


def _count_csv_rows(text: str, dialect) -> int:
    """Count data rows in a CSV string, handling embedded newlines in quoted fields."""
    try:
        reader = csv.reader(io.StringIO(text), dialect=dialect)
        # Skip header row if the CSV has column names
        header = next(reader, None)
        if header is None:
            return 0
        return sum(1 for _ in reader)
    except Exception:
        # Fallback for malformed content
        return max(0, len(text.splitlines()) - 1)


def detect_encoding(file_bytes: bytes) -> str:
    """Try multiple encodings until one works for the entire content."""
    for enc in ENCODINGS:
        try:
            file_bytes.decode(enc)
            return enc
        except (UnicodeDecodeError, UnicodeError):
            continue
    return "latin-1"


def detect_columns(file_bytes: bytes) -> dict:
    """
    Detect columns in a CSV file.
    Returns dict with 'columns' list and 'sample_rows' (first 5 rows).
    """
    encoding = detect_encoding(file_bytes)
    text = file_bytes.decode(encoding)
    text_io = io.StringIO(text)

    # Detect delimiter
    sample = text_io.read(4096)
    text_io.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel  # fallback to comma-separated

    reader = csv.DictReader(text_io, dialect=dialect)
    if not reader.fieldnames:
        return {"columns": [], "sample_rows": [], "encoding": encoding}

    columns = [f.strip() for f in reader.fieldnames if f]

    # Read first 5 rows as sample
    sample_rows = []
    for i, row in enumerate(reader):
        if i >= 5:
            break
        cleaned = {k.strip(): (v.strip() if isinstance(v, str) else ("" if v is None else str(v))) for k, v in row.items() if k}
        sample_rows.append(cleaned)

    return {
        "columns": columns,
        "sample_rows": sample_rows,
        "encoding": encoding,
        "row_count_estimate": _count_csv_rows(text, dialect),
    }


def convert_csv(
    file_bytes: bytes,
    email_column: str,
    name_column: Optional[str] = None,
    attribute_columns: Optional[list[str]] = None,
) -> dict:
    """
    Convert a CSV file to ListMonk-compatible format.

    Args:
        file_bytes: Raw CSV file content
        email_column: Name of the column containing email addresses
        name_column: Name of the column containing subscriber names (optional)
        attribute_columns: List of column names to include as JSON attributes (optional)

    Returns:
        dict with 'csv_content' (converted CSV as string), 'stats' (conversion stats)
    """
    encoding = detect_encoding(file_bytes)
    text = file_bytes.decode(encoding)
    text_io = io.StringIO(text)

    # Detect delimiter
    sample = text_io.read(4096)
    text_io.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(text_io, dialect=dialect)
    if not reader.fieldnames:
        return {"csv_content": "", "stats": {"error": "No columns found in CSV"}}

    reader.fieldnames = [f.strip() for f in reader.fieldnames if f]

    # Build case-insensitive column mapping
    col_map = {f.strip().lower(): f for f in reader.fieldnames if f}

    # Validate email column
    actual_email = col_map.get(email_column.strip().lower())
    if not actual_email:
        return {
            "csv_content": "",
            "stats": {
                "error": f"Email column '{email_column}' not found",
                "available_columns": reader.fieldnames,
            },
        }

    # Resolve name column
    actual_name = None
    if name_column:
        actual_name = col_map.get(name_column.strip().lower())

    # Resolve attribute columns
    actual_attribs = {}
    if attribute_columns:
        for attr in attribute_columns:
            actual = col_map.get(attr.strip().lower())
            if actual:
                actual_attribs[attr.strip()] = actual

    # Convert
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["email", "name", "attributes"])
    writer.writeheader()

    success = 0
    skipped = 0

    for row in reader:
        row = {k.strip(): (v.strip() if isinstance(v, str) else ("" if v is None else str(v))) for k, v in row.items() if k}

        email = row.get(actual_email, "").strip()
        if not email:
            skipped += 1
            continue

        name = ""
        if actual_name:
            name = row.get(actual_name, "").strip()

        attribs = {}
        for attr_key, csv_col in actual_attribs.items():
            val = row.get(csv_col, "").strip()
            if val:
                attribs[attr_key] = val

        writer.writerow({
            "email": email,
            "name": name,
            "attributes": json.dumps(attribs, ensure_ascii=False) if attribs else "{}",
        })
        success += 1

    return {
        "csv_content": output.getvalue(),
        "stats": {
            "converted": success,
            "skipped": skipped,
            "total": success + skipped,
            "encoding_detected": encoding,
        },
    }
