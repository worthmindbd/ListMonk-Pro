"""
CSV to ListMonk format converter.
Refactored from the standalone convert.py script.

ListMonk requires subscribers CSV with columns: email, name, attributes
where attributes is a JSON string of extra fields.
"""

import csv
import io
import json
import re
from typing import Optional


ENCODINGS = ["utf-8-sig", "utf-8", "latin-1", "cp1252"]

# Header-name hints for guessing which column is which. Compared after
# stripping every non-alphanumeric character and lowercasing, so
# "E-Mail Address" and "email_address" both normalise to "emailaddress".
_EMAIL_HEADER_EXACT = {"email", "emailaddress", "emailid", "mail", "email1", "primaryemail"}
_NAME_HEADER_EXACT = {"name", "fullname", "contactname", "firstname", "lastname", "contactperson", "contact"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)


def _normalise_header(header: str) -> str:
    """Lowercase a header and drop everything that is not alphanumeric."""
    return re.sub(r"[^a-z0-9]", "", header.lower())


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


def _count_ragged_rows(text: str, dialect, expected: int) -> int:
    """Count data rows whose field count differs from the header length.

    Such rows are still converted (a trailing/leading empty field is common and
    usually harmless), but they can shift values into the wrong column, so the
    caller surfaces the count as a warning instead of failing silently.
    """
    if expected <= 0:
        return 0
    bad = 0
    try:
        reader = csv.reader(io.StringIO(text), dialect=dialect)
        next(reader, None)
        for row in reader:
            if not row or (len(row) == 1 and not row[0].strip()):
                continue
            if len(row) != expected:
                bad += 1
    except Exception:
        return bad
    return bad


def suggest_columns(columns: list[str], sample_rows: list[dict]) -> dict:
    """Best-effort guess of which CSV columns map to ListMonk's email/name.

    Header names are checked first, then the sample values themselves (a column
    that actually contains addresses wins over an unrecognised header).
    Returns ``{"email": col|None, "name": col|None}``.
    """
    norm = {c: _normalise_header(c) for c in columns}
    email = None
    name = None

    for col, n in norm.items():
        if n in _EMAIL_HEADER_EXACT:
            email = col
            break
    if email is None:
        for col, n in norm.items():
            if "email" in n or n.endswith("mail"):
                email = col
                break

    if email is None and sample_rows:
        best_ratio, best_col = 0.0, None
        for col in columns:
            values = [str(row.get(col) or "").strip() for row in sample_rows]
            values = [v for v in values if v]
            if not values:
                continue
            ratio = sum(1 for v in values if _EMAIL_RE.match(v)) / len(values)
            if ratio >= 0.6 and ratio > best_ratio:
                best_ratio, best_col = ratio, col
        email = best_col

    for col, n in norm.items():
        if col != email and n in _NAME_HEADER_EXACT:
            name = col
            break
    if name is None:
        for col, n in norm.items():
            if col != email and (n.endswith("name") or n.startswith("name")):
                name = col
                break

    return {"email": email, "name": name}


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

    raw_fieldnames = [f for f in reader.fieldnames if f]
    columns = [f.strip() for f in raw_fieldnames]

    # Read first 5 rows as sample
    sample_rows = []
    for i, row in enumerate(reader):
        if i >= 5:
            break
        cleaned = {k.strip(): (v.strip() if isinstance(v, str) else ("" if v is None else str(v))) for k, v in row.items() if k}
        sample_rows.append(cleaned)

    ragged_rows = _count_ragged_rows(text, dialect, len(columns))
    warnings = []
    if ragged_rows:
        warnings.append(
            f"{ragged_rows} row(s) have a different number of fields than the header. "
            "Those rows may be shifted or skipped - check the preview before importing."
        )

    return {
        "columns": columns,
        "sample_rows": sample_rows,
        "encoding": encoding,
        "row_count_estimate": _count_csv_rows(text, dialect),
        "suggested": suggest_columns(columns, sample_rows),
        "ragged_rows": ragged_rows,
        "warnings": warnings,
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

    ragged_rows = _count_ragged_rows(text, dialect, len([f for f in reader.fieldnames if f]))
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
            "ragged_rows": ragged_rows,
        },
    }
