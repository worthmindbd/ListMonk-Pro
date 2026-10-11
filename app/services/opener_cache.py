"""
Cache of campaign opener emails (campaign_views) to avoid re-fetching tens of
thousands of subscribers on every bounce list request.
"""

import asyncio
import logging
import time
from app.services.listmonk_client import ListMonkClient

logger = logging.getLogger("opener_cache")

_cache: dict[int, set[str]] = {}
_fetched_at: dict[int, float] = {}
_inflight: dict[int, asyncio.Task] = {}
_lock = asyncio.Lock()

TTL_SECONDS = 10 * 60  # 10 minutes


def _views_query(campaign_id: int) -> str:
    return (
        f"subscribers.id IN (SELECT subscriber_id FROM campaign_views "
        f"WHERE campaign_id={campaign_id})"
    )


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


async def _fetch_opener_emails(client: ListMonkClient, campaign_id: int) -> set[str]:
    subs = await client.paginate_all(
        client.get_subscribers, per_page=500,
        query=_views_query(campaign_id),
    )
    emails = {_normalize_email(s["email"]) for s in subs if s.get("email")}
    _cache[campaign_id] = emails
    _fetched_at[campaign_id] = time.monotonic()
    logger.info("Opener cache updated for campaign %s: %d emails", campaign_id, len(emails))
    return emails


async def get_opener_emails(
    client: ListMonkClient, campaign_id: int, *, force_refresh: bool = False,
) -> set[str]:
    """Return cached opener emails for a campaign, refreshing after TTL.

    The lock only guards the in-flight registry — the network fetch itself
    runs outside it, so lookups for different campaigns do not serialize.
    """
    if not force_refresh and is_cached(campaign_id):
        return _cache[campaign_id]

    # Reuse an in-flight fetch instead of issuing a duplicate request.
    async with _lock:
        task = _inflight.get(campaign_id)
        if task is None:
            task = asyncio.create_task(_fetch_opener_emails(client, campaign_id))
            _inflight[campaign_id] = task

    try:
        return await task
    finally:
        async with _lock:
            if _inflight.get(campaign_id) is task:
                _inflight.pop(campaign_id, None)


async def get_opener_emails_for_campaigns(
    client: ListMonkClient, campaign_ids: set[int],
    concurrency: int = 10,
) -> dict[int, set[str]]:
    """Fetch opener sets for many campaigns with bounded concurrency."""
    if not campaign_ids:
        return {}
    sem = asyncio.Semaphore(concurrency)

    async def fetch(cid: int) -> tuple[int, set[str]]:
        async with sem:
            return cid, await get_opener_emails(client, cid)

    pairs = await asyncio.gather(*(fetch(cid) for cid in campaign_ids))
    return dict(pairs)


def invalidate(campaign_id: int | None = None) -> None:
    """Clear opener cache (all campaigns or one)."""
    if campaign_id is None:
        _cache.clear()
        _fetched_at.clear()
        return
    _cache.pop(campaign_id, None)
    _fetched_at.pop(campaign_id, None)


def is_cached(campaign_id: int) -> bool:
    if campaign_id not in _cache:
        return False
    return time.monotonic() - _fetched_at.get(campaign_id, 0) < TTL_SECONDS


def get_cached_opener_emails(campaign_id: int) -> set[str] | None:
    if is_cached(campaign_id):
        return _cache[campaign_id]
    return None
