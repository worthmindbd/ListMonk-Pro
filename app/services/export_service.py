import csv
import io
import json
from typing import Any, AsyncIterator, Iterator


def _flatten_row(row: dict, columns: list[str]) -> dict:
    """Flatten nested dicts/lists to JSON strings so CSV can hold them."""
    flat_row = {}
    for col in columns:
        val = row.get(col, "")
        if isinstance(val, (dict, list)):
            flat_row[col] = json.dumps(val, ensure_ascii=False)
        else:
            flat_row[col] = val
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
