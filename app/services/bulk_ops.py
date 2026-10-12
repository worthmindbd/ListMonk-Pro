"""Shared helpers for bulk actions ListMonk only exposes per item.

ListMonk's REST API has no bulk delete/archive for lists, campaigns and
templates, so those pages loop server-side. Keeping the loop here means every
bulk endpoint gets the same ID validation, the same concurrency bound (a
"select all" must not open 500 simultaneous upstream requests) and the same
partial-failure reporting instead of three slightly different copies.
"""

import asyncio
import logging

logger = logging.getLogger("bulk_ops")

# Upstream is responsive but not free: 5 concurrent per-item calls keep a
# bulk action snappy without stampeding ListMonk's connection pool.
DEFAULT_CONCURRENCY = 5

# Cap the client-supplied ID list. The UI can only select one page at a time
# (<= 100 rows), so a larger payload is a bug or an attempt to turn one request
# into an unbounded upstream walk.
MAX_BULK_IDS = 500


def parse_ids(raw, limit: int = MAX_BULK_IDS) -> list[int]:
    """Validate a client-supplied ID payload into a deduped list of ints.

    Raises ValueError (routers turn that into a 400) rather than coercing
    junk to 0, so a malformed body can never silently act on everything.
    """
    if raw is None or raw == "":
        return []
    if not isinstance(raw, (list, tuple, set)):
        raise ValueError("ids must be a list of numbers")
    if len(raw) > limit:
        raise ValueError(f"Too many ids (max {limit})")
    seen: set[int] = set()
    ids: list[int] = []
    for value in raw:
        try:
            item = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid id: {value!r}")
        if item < 1:
            raise ValueError(f"Invalid id: {value!r}")
        if item not in seen:
            seen.add(item)
            ids.append(item)
    return ids


async def run_bulk(items, op, concurrency: int = DEFAULT_CONCURRENCY) -> dict:
    """Call ``op(item)`` once per item with bounded concurrency.

    Returns ``{"ok": n, "errors": m, "failed": [{"id", "error"}]}`` so a UI can
    report partial failures instead of claiming a clean sweep.
    """
    sem = asyncio.Semaphore(concurrency)
    result = {"ok": 0, "errors": 0, "failed": []}

    async def run(item):
        async with sem:
            try:
                await op(item)
                result["ok"] += 1
            except Exception as exc:
                result["errors"] += 1
                result["failed"].append({"id": item, "error": str(exc)})
                logger.error(f"bulk action failed for {item}: {exc}")

    await asyncio.gather(*(run(item) for item in items))
    return result