"""
CSV to ListMonk format converter.
Refactored from the standalone convert.py script.

ListMonk requires subscribers CSV with columns: email, name, attributes
where attributes is a JSON string of extra fields.

ListMonk's importer (internal/subimporter) only understands the headers
``email``, ``name`` and ``attributes``: every other header is ignored, the
``email`` column is mandatory, and a row with malformed JSON in ``attributes``
is dropped from the import. The converter therefore maps arbitrary source
columns onto those three headers, promoting every unclaimed column into the
``attributes`` JSON object so no source data is lost on the way in.
"""

import csv
import io
import json
import re
import sys
from typing import Optional

# One oversized cell (a big JSON blob, embedded HTML, base64 — plausible for a
# converter that promotes every column into attributes) must not kill the whole
# conversion with "field larger than field limit": the csv module's default
# 131,072-char cap exists to stop pathological memory use, not real data.
try:
    csv.field_size_limit(sys.maxsize)
except (OverflowError, ValueError):  # pragma: no cover - 32-bit platforms
    csv.field_size_limit(2**31 - 1)


ENCODINGS = ["utf-8-sig", "utf-8", "latin-1", "cp1252"]

# Header-name hints for guessing which column is which. Compared after
# stripping every non-alphanumeric character and lowercasing, so
# "E-Mail Address" and "email_address" both normalise to "emailaddress".
_EMAIL_HEADER_EXACT = {"email", "emailaddress", "emailid", "mail", "email1", "primaryemail"}
_NAME_HEADER_EXACT = {"name", "fullname", "contactname", "firstname", "lastname", "contactperson", "contact"}
# Matches anything ListMonk's importer would accept as a bare address: one
# "@", no whitespace on either side, and a dotted domain (no leading/trailing
# dot). It is intentionally more permissive than :data:`_EMAIL_STRICT_RE`
# (which decides column *suggestions*), so a valid-but-unusual address
# (unicode domain, single-character TLD) is never dropped on the way in.
_EMAIL_RE = re.compile(r"^[^@\s]+@(?:[^@\s.]+\.)+[^@\s.]+$", re.IGNORECASE)
# Deliberately strict: requires a dotted domain with a 2+ letter TLD. Used to
# score sample values, so a column full of "bob@company" typos is not picked as
# the email column.
_EMAIL_STRICT_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)


def _normalise_header(header: str) -> str:
    """Lowercase a header and drop everything that is not alphanumeric."""
    return re.sub(r"[^a-z0-9]", "", header.lower())


def _is_valid_email(value: str) -> bool:
    """Whether a cell looks like a bare, importable e-mail address.

    Mirrors ListMonk's own validation closely enough that the converter never
    drops an address the importer would accept.
    """
    return bool(_EMAIL_RE.match((value or "").strip()))


def _normalise_email(value: str) -> str:
    """Trim and lowercase, matching the canonical form ListMonk stores."""
    return value.strip().lower()


def _merge_json_attr(attribs: dict, value: str) -> None:
    """Merge a cell containing a JSON object into the attributes dict.

    ListMonk stores nested attributes by querying them as JSON paths, so a
    source cell such as ``{"age": 30}`` is imported merged into the row's
    attributes instead of being stringified. Non-JSON cells are ignored.
    """
    value = value.strip()
    if not (value.startswith("{") and value.endswith("}")):
        return
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return
    if isinstance(parsed, dict):
        for key, val in parsed.items():
            attribs.setdefault(str(key), val)


def _count_csv_rows(text: str, delimiter: str) -> int:
    """Count data rows in a CSV string, handling embedded newlines in quoted fields."""
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        # Skip header row if the CSV has column names
        header = next(reader, None)
        if header is None:
            return 0
        return sum(1 for _ in reader)
    except Exception:
        # Fallback for malformed content
        return max(0, len(text.splitlines()) - 1)


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
            ratio = sum(1 for v in values if _EMAIL_STRICT_RE.match(v)) / len(values)
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


_DELIMITERS = [",", "\t", ";", "|"]


def _delimiter_score(text: str, delim: str) -> int:
    """Score how well a delimiter splits the sample: columns minus inconsistency."""
    try:
        rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim) if r]
    except csv.Error:
        return 0
    if not rows:
        return 0
    counts = [len(r) for r in rows[:50]]
    if max(counts) <= 1:
        return 0
    # Reward column count, punish rows that break the file's consistency.
    return max(counts) - (max(counts) - min(counts))


def _detect_delimiter(text: str) -> str:
    """Pick the delimiter that parses the file most consistently.

    ``csv.Sniffer`` gives up on ordinary European "a;b;c" exports and can guess
    a separator that only appears inside values, so its suggestion is accepted
    only when it parses as well as the best candidate measured across the first
    rows. ``,`` is the tie-breaker, which keeps single-column files unchanged.
    """
    sample = text[:65536]
    try:
        sniffed = csv.Sniffer().sniff(sample, delimiters="".join(_DELIMITERS)).delimiter
    except csv.Error:
        sniffed = ""

    candidates = [(d, _delimiter_score(sample, d)) for d in _DELIMITERS]
    if sniffed in _DELIMITERS:
        # Comma is evaluated last, so a sniffer guess wins ties but never a
        # file that another delimiter splits more reliably.
        candidates = [c for c in candidates if c[0] != sniffed] + [(sniffed, _delimiter_score(sample, sniffed))]
    best_delim, best_score = ",", 0
    for delim, score in candidates:
        if score >= best_score and score > 0:
            best_delim, best_score = delim, score
    return best_delim


def _prepare_reader(file_bytes: bytes) -> tuple[str, str, list[str], io.StringIO, str]:
    """Decode the upload and detect its delimiter and header row.

    Returns ``(text, delimiter, headers, text_io, encoding)`` where ``headers``
    keeps a stripped placeholder for **every** physical column — blank header
    cells become ``""`` — so a column's index in this list is also its index in
    the raw data rows, and ``text_io`` is positioned at the first data row,
    ready for a ``csv.DictReader``. Filtering the blanks out here used to
    compact the positions, so with a header like ``,name,email`` (pandas
    ``to_csv(index=True)``) the email column resolved onto the index-number
    column and every row was mis-mapped or dropped.

    ``encoding`` is returned so callers that report it in stats do not have to
    run the (expensive) detection a second time over the same bytes.
    """
    encoding = detect_encoding(file_bytes)
    text = file_bytes.decode(encoding)
    text_io = io.StringIO(text)

    delimiter = _detect_delimiter(text)
    reader = csv.reader(text_io, delimiter=delimiter)
    try:
        raw_header = next(reader, None) or []
    except csv.Error:
        raw_header = []
    headers = [str(h).strip() for h in raw_header]
    return text, delimiter, headers, text_io, encoding


def _column_index_map(headers: list[str]) -> dict[str, int]:
    """Map lowercase header names to their position in the file.

    Blank placeholder entries (physically present columns with an empty
    header cell) are skipped so an empty name can never resolve to a column.
    Duplicate headers keep the first position (readers naturally use the
    left-most column of a repeated name).
    """
    mapping: dict[str, int] = {}
    for index, header in enumerate(headers):
        if header:
            mapping.setdefault(header.lower(), index)
    return mapping


def _resolve_column(user_column: Optional[str], index_map: dict[str, int], headers: list[str]) -> Optional[int]:
    """Resolve a user-supplied column name to its file position.

    Matches case-insensitively on the stripped name. Must be called *before*
    row cleaning strips and trims the row dicts, so keys stay as detected.
    """
    if not user_column:
        return None
    key = str(user_column).strip().lower()
    if key in index_map:
        return index_map[key]
    # Tolerate selections created before headers were normalised.
    for index, header in enumerate(headers):
        if header.strip().lower() == key:
            return index
    return None


def _attribute_plan(
    headers: list[str],
    index_map: dict[str, int],
    email_idx: int,
    name_idx: Optional[int],
    attribute_columns: Optional[list[str]],
    auto_attributes: bool,
) -> tuple[list[tuple[str, int]], list[tuple[str, int]], list[str]]:
    """Work out which columns become attributes.

    Returns ``(selected, auto, unresolved)`` where ``selected``/``auto`` are
    pairs of ``(attribute key, column index)`` and ``unresolved`` holds the
    requested column names that do not exist in this file. ``selected`` holds
    the user's explicit picks (always applied). ``auto`` holds every remaining
    column, so nothing in the file is dropped. Keys are deduplicated
    case-insensitively, which also keeps a picked column out of the auto list.

    Unresolved picks are reported instead of being skipped silently: a stale
    selection (mapping carried over from another file) that quietly resolves to
    nothing is exactly how an import ends up storing empty attributes.
    """
    selected: list[tuple[str, int]] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for attr in attribute_columns or []:
        idx = _resolve_column(attr, index_map, headers)
        if idx is None:
            unresolved.append(str(attr))
            continue
        key = str(attr).strip()
        if key.lower() in seen:
            continue
        seen.add(key.lower())
        selected.append((key, idx))

    # Columns never promoted: the email and name columns (their values land in
    # ``email`` / ``name``) and anything already picked explicitly.
    skip = {email_idx, *(idx for _, idx in selected)}
    if name_idx is not None:
        skip.add(name_idx)

    auto: list[tuple[str, int]] = []
    if auto_attributes:
        for idx, header in enumerate(headers):
            # Blank header cells are positional placeholders only: with no
            # name there is nothing to promote (and no key to store it under).
            if not header or idx in skip or header.lower() in seen:
                continue
            seen.add(header.lower())
            auto.append((header, idx))

    return selected, auto, unresolved


def _droppable_columns(headers: list[str], email_idx: int, name_idx: Optional[int]) -> list[str]:
    """Columns that carry data but are not mapped to email/name.

    In ``selected`` attribute mode these are the columns the user is choosing
    to throw away; the converter refuses to do that silently when the choice
    is "none of them". Blank placeholder columns have no name to show, so they
    are excluded from the message.
    """
    skip = {email_idx}
    if name_idx is not None:
        skip.add(name_idx)
    return [h for idx, h in enumerate(headers) if idx not in skip and h]


def detect_columns(file_bytes: bytes) -> dict:
    """
    Detect columns in a CSV file.
    Returns dict with 'columns' list and 'sample_rows' (first 5 rows).
    """
    text, delimiter, headers, text_io, encoding = _prepare_reader(file_bytes)

    if not any(headers):
        return {"columns": [], "sample_rows": [], "encoding": encoding}
    # Blank header cells stay in ``headers`` as positional placeholders; the
    # UI column picker only shows names a user could actually select.
    named_columns = [h for h in headers if h]

    # _prepare_reader consumed the header row; rewind so DictReader re-reads it.
    text_io.seek(0)
    reader = csv.DictReader(text_io, delimiter=delimiter)

    # Read first 5 rows as sample
    sample_rows = []
    for i, row in enumerate(reader):
        if i >= 5:
            break
        cleaned = {k.strip(): (v.strip() if isinstance(v, str) else ("" if v is None else str(v))) for k, v in row.items() if k}
        sample_rows.append(cleaned)

    # Count rows whose field count differs from the header, so a shifted row is
    # visible in the preview instead of silently importing wrong values.
    ragged_rows = 0
    try:
        counter = csv.reader(io.StringIO(text), delimiter=delimiter)
        next(counter, None)
        for row in counter:
            # Blank lines aren't data rows, so they aren't ragged either.
            if not any((c or "").strip() for c in row):
                continue
            if len(row) != len(headers):
                ragged_rows += 1
    except Exception:
        pass
    warnings = []
    if ragged_rows:
        warnings.append(
            f"{ragged_rows} row(s) have a different number of fields than the header. "
            "Those rows may be shifted or skipped - check the preview before importing."
        )

    return {
        "columns": named_columns,
        "sample_rows": sample_rows,
        "encoding": encoding,
        "row_count_estimate": _count_csv_rows(text, delimiter),
        "suggested": suggest_columns(named_columns, sample_rows),
        "ragged_rows": ragged_rows,
        "warnings": warnings,
    }


def _row_cell(row: list, idx: int) -> str:
    """Stripped cell value, empty when the row is short (ragged input)."""
    if idx >= len(row):
        return ""
    value = row[idx]
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value).strip()


def _delimiter_label(delimiter: str) -> str:
    """Human-readable name for a delimiter character."""
    return {",": "comma", "\t": "tab", ";": "semicolon", "|": "pipe"}.get(delimiter, delimiter)


def _iter_converted_rows(
    text: str,
    delimiter: str,
    delimiter_label: str,
    headers: list[str],
    email_idx: int,
    name_idx: Optional[int],
    selected_attribs: list[tuple[str, int]],
    auto_attribs: list[tuple[str, int]],
):
    """Yield converted ListMonk rows ``(email, name, attributes_json, stats)``.

    Every yielded row carries the stats accumulated so far (the same dict object
    updated in place), and a final stats-only sentinel is emitted once the file
    is drained. That lets ``convert_csv`` write rows while ``preview_conversion``
    only keeps the first few, both from one conversion implementation.
    """
    counts = {"converted": 0, "empty_emails": 0, "invalid_emails": 0, "ragged_rows": 0, "empty_attributes": 0}
    stats = {
        "converted": 0,
        "skipped": 0,
        "total": 0,
        "empty_emails": 0,
        "invalid_emails": 0,
        "ragged_rows": 0,
        "empty_attributes": 0,
        "delimiter": delimiter_label,
        "attribute_columns": [key for key, _ in selected_attribs + auto_attribs],
        "attribute_count": len(selected_attribs) + len(auto_attribs),
        "attributes_empty": not (selected_attribs or auto_attribs),
        "auto_attributes": bool(auto_attribs),
    }

    def flush_stats() -> None:
        stats.update(
            converted=counts["converted"],
            skipped=counts["empty_emails"] + counts["invalid_emails"],
            total=counts["converted"] + counts["empty_emails"] + counts["invalid_emails"],
            empty_emails=counts["empty_emails"],
            invalid_emails=counts["invalid_emails"],
            ragged_rows=counts["ragged_rows"],
            empty_attributes=counts["empty_attributes"],
        )

    row_reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    next(row_reader, None)

    for raw in row_reader:
        # Skip fully blank lines.
        if not raw or (len(raw) == 1 and not raw[0].strip()):
            continue
        if len(raw) != len(headers):
            counts["ragged_rows"] += 1

        email = _row_cell(raw, email_idx)
        if not email:
            counts["empty_emails"] += 1
            continue
        if not _is_valid_email(email):
            # ListMonk skips malformed addresses during import, so skip them
            # here where they can be reported to the user.
            counts["invalid_emails"] += 1
            continue
        # ListMonk lowercases addresses on import; matching that here keeps the
        # preview honest and makes duplicate handling predictable.
        email = _normalise_email(email)

        name = _row_cell(raw, name_idx) if name_idx is not None else ""

        attribs: dict = {}
        for key, idx in selected_attribs + auto_attribs:
            value = _row_cell(raw, idx)
            if value:
                attribs[key] = value
                _merge_json_attr(attribs, value)
        # A row can be converted and still carry no attribute values at all
        # (every mapped column blank). Counting them lets the UI warn that the
        # import will store email/name only, instead of claiming full fidelity.
        if not attribs:
            counts["empty_attributes"] += 1

        counts["converted"] += 1
        flush_stats()
        yield email, name, (json.dumps(attribs, ensure_ascii=False) if attribs else "{}"), stats

    # Flush once more for skipped rows (which yield nothing) and emit a final
    # stats-only sentinel so callers receive complete stats even when every row
    # was skipped. The empty email marks it.
    flush_stats()
    yield "", "", "", stats


def _conversion_context(
    file_bytes: bytes,
    email_column: str,
    name_column: Optional[str],
    attribute_columns: Optional[list[str]],
    auto_attributes: bool,
) -> tuple[Optional[dict], Optional[dict]]:
    """Validate the mapping and prepare the row generator.

    Returns ``(error, context)``: an ``{"error": ...}`` stats dict when the
    mapping is unusable (or the file has no columns), otherwise ``None`` and a
    context dict with ``text``, ``delimiter``, ``headers``, ``email_idx``,
    ``name_idx``, ``selected``, ``auto`` and ``unresolved`` — everything
    ``_iter_converted_rows`` and the stats reporting need.

    A mapping that would throw every unmapped column away (``selected`` mode
    with no usable attribute column while the file still holds data columns)
    is rejected here rather than converted into a file of empty attributes:
    that failure used to import "successfully" while silently dropping all
    of the source data.
    """
    text, delimiter, headers, _, encoding = _prepare_reader(file_bytes)
    if not any(headers):
        return {"error": "No columns found in CSV"}, None

    index_map = _column_index_map(headers)
    email_idx = _resolve_column(email_column, index_map, headers)
    if email_idx is None:
        return {"error": f"Email column '{email_column}' not found"}, None

    name_idx = _resolve_column(name_column, index_map, headers)
    selected, auto, unresolved = _attribute_plan(
        headers, index_map, email_idx, name_idx, attribute_columns, auto_attributes,
    )

    if not auto_attributes and not selected:
        droppable = _droppable_columns(headers, email_idx, name_idx)
        if attribute_columns and unresolved:
            return {
                "error": (
                    f"None of the selected attribute columns exist in this file: "
                    f"{', '.join(unresolved[:10])}. Re-map the columns for this upload."
                ),
                "unresolved_columns": unresolved,
            }, None
        if droppable:
            shown = ", ".join(droppable[:6]) + ("…" if len(droppable) > 6 else "")
            return {
                "error": (
                    f"No attribute columns selected: {len(droppable)} column(s) would be "
                    f"dropped ({shown}). Pick the columns to store as attributes, or turn "
                    "'convert all other columns to attributes' back on."
                ),
                "code": "no_attributes",
                "unresolved_columns": unresolved,
            }, None

    return None, {
        "text": text,
        "delimiter": delimiter,
        "headers": headers,
        "email_idx": email_idx,
        "name_idx": name_idx,
        "selected": selected,
        "auto": auto,
        "unresolved": unresolved,
        "encoding": encoding,
    }


def convert_csv(
    file_bytes: bytes,
    email_column: str,
    name_column: Optional[str] = None,
    attribute_columns: Optional[list[str]] = None,
    auto_attributes: bool = True,
) -> dict:
    """
    Convert a CSV file to ListMonk-compatible format.

    Output is always the three ListMonk headers: ``email``, ``name`` and
    ``attributes`` (a JSON string).

    Args:
        file_bytes: Raw CSV file content
        email_column: Name of the column containing email addresses
        name_column: Name of the column containing subscriber names (optional)
        attribute_columns: Explicit column names to include as JSON attributes
        auto_attributes: When True, every column that is not the email or name
            column and was not explicitly listed becomes an attribute too, so
            an arbitrary CSV is fully preserved without manual mapping. When
            attribute_columns is given, those entries are always included.

    Returns:
        dict with 'csv_content' (converted CSV as string), 'stats' (conversion stats)
    """
    error, ctx = _conversion_context(
        file_bytes, email_column, name_column, attribute_columns, auto_attributes,
    )
    if error:
        return {"csv_content": "", "stats": error}

    encoding = ctx["encoding"]

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["email", "name", "attributes"])
    writer.writeheader()

    stats: dict = {}
    for email, name, attributes, row_stats in _iter_converted_rows(
        ctx["text"], ctx["delimiter"], _delimiter_label(ctx["delimiter"]), ctx["headers"],
        ctx["email_idx"], ctx["name_idx"], ctx["selected"], ctx["auto"],
    ):
        stats = row_stats
        # The empty email marks the final stats-only sentinel.
        if not email:
            continue
        writer.writerow({"email": email, "name": name, "attributes": attributes})

    stats["encoding_detected"] = encoding
    stats["unresolved_columns"] = ctx["unresolved"]
    return {"csv_content": output.getvalue(), "stats": stats}


def preview_conversion(
    file_bytes: bytes,
    email_column: str,
    name_column: Optional[str] = None,
    attribute_columns: Optional[list[str]] = None,
    auto_attributes: bool = True,
    limit: int = 10,
) -> dict:
    """Convert a CSV and return the first ``limit`` output rows as dicts.

    Lets the UI show exactly what ListMonk will receive (email, name and the
    attributes JSON) without downloading the file. ``stats`` describe the full
    conversion, not just the previewed slice: enough rows are collected for the
    preview while the rest of the file is still scanned to keep stats honest.
    """
    error, ctx = _conversion_context(
        file_bytes, email_column, name_column, attribute_columns, auto_attributes,
    )
    if error:
        return {"rows": [], "stats": error}

    encoding = ctx["encoding"]

    take = max(0, limit)
    rows: list[dict] = []
    stats: dict = {}
    for email, name, attributes, row_stats in _iter_converted_rows(
        ctx["text"], ctx["delimiter"], _delimiter_label(ctx["delimiter"]), ctx["headers"],
        ctx["email_idx"], ctx["name_idx"], ctx["selected"], ctx["auto"],
    ):
        stats = row_stats
        # The empty email marks the final stats-only sentinel.
        if not email:
            continue
        if len(rows) < take:
            rows.append({"email": email, "name": name, "attributes": attributes})

    stats["encoding_detected"] = encoding
    stats["unresolved_columns"] = ctx["unresolved"]
    return {"rows": rows, "stats": stats}
