import csv
import io
import json
from typing import Any, AsyncIterator, Iterator

# Leading characters that make spreadsheet applications (Excel, LibreOffice,
# Google Sheets) interpret a CSV cell as a formula instead of text. Cell
# values like subscriber names or reply bodies are attacker-controllable, so
# every exported string cell is neutralised before it reaches the writer.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_cell(value: Any) -> Any:
    """Defuse CSV formula injection in string cells.

    A leading single quote makes spreadsheet applications treat the cell as
    literal text; the quote is not displayed. Non-string values (ids, counts,
    timestamps as numbers) are returned unchanged. Leading whitespace is
    stripped before checking so ``" =SUM(A1)"`` cannot bypass the filter.
    """
    if isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith(_FORMULA_PREFIXES):
            return "'" + value
    return value


def _flatten_row(row: dict, columns: list[str]) -> dict:
    """Flatten nested dicts/lists to JSON strings so CSV can hold them.

    Every cell also passes through ``_sanitize_cell`` — both writers go via
    this helper, which is the single place exports can be formula-injected.
    """
    flat_row = {}
    for col in columns:
        val = row.get(col, "")
        if isinstance(val, (dict, list)):
            val = json.dumps(val, ensure_ascii=False)
        flat_row[col] = _sanitize_cell(val)
    return flat_row


def dict_list_to_csv(data: list[dict], columns: list[str]) -> Iterator[str]:
    """Convert an in-memory list of dicts to CSV string chunks.

    Note this still requires the whole dataset in memory; prefer
    ``aiter_dicts_to_csv`` when the rows can be streamed from the API.
    """
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    yield output.getvalue()
    output.seek(0)
    output.truncate(0)

    for row in data:
        writer.writerow(_flatten_row(row, columns))
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)


async def aiter_dicts_to_csv(rows: AsyncIterator[dict], columns: list[str]) -> AsyncIterator[str]:
    """Stream CSV chunks from an async row iterator.

    Rows are consumed lazily, so peak memory stays proportional to a single
    page rather than the whole export. Each row is flushed as its own chunk,
    which keeps the response streaming instead of buffering the full CSV.
    """
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    yield output.getvalue()
    output.seek(0)
    output.truncate(0)

    async for row in rows:
        writer.writerow(_flatten_row(row, columns))
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)
