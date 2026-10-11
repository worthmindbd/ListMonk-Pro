"""
Hard bounce cache: maintains counts of hard bounces per campaign.
Updated periodically in background to avoid slow on-demand fetching.
"""

import asyncio
import logging
from app.services.listmonk_client import listmonk
from app.services.bounce_filters import filter_bounces_excluding_openers, bounce_campaign_id
from app.services.task_utils import spawn

logger = logging.getLogger("hard_bounce_cache")

# Cache: {campaign_id: hard_bounce_count}
_hard_bounce_counts: dict[int, int] = {}
_last_updated: str = ""

BATCH_SIZE = 500
UPDATE_INTERVAL = 5 * 60  # 5 minutes

# Serialises refreshes so two overlapping runs cannot interleave their
# full-table pagination, and coalesces ad-hoc refresh requests.
_update_lock = asyncio.Lock()
_pending_update: asyncio.Task | None = None


def _is_client_ready() -> bool:
    """Check if ListMonk client is ready (has valid HTTP client)."""
    return listmonk._client is not None


async def update_hard_bounce_counts():
    """Fetch all bounces and count hard bounces per campaign."""
    global _hard_bounce_counts, _last_updated
    from datetime import datetime

    async with _update_lock:
        try:
            logger.info("Updating hard bounce counts...")
            all_bounces = await listmonk.paginate_all(
                listmonk.get_bounces, per_page=BATCH_SIZE, bounce_type="hard",
            )
            hard_bounces = [b for b in all_bounces if b.get("type") == "hard"]
            hard_bounces = await filter_bounces_excluding_openers(listmonk, hard_bounces)

            counts: dict[int, int] = {}
            for b in hard_bounces:
                cid = bounce_campaign_id(b)
                if cid:
                    counts[cid] = counts.get(cid, 0) + 1

            _hard_bounce_counts = counts
            _last_updated = datetime.now().isoformat()
            logger.info(f"Hard bounce counts updated: {len(counts)} campaigns")
        except Exception as e:
            logger.error(f"Failed to update hard bounce counts: {e}", exc_info=True)


def schedule_hard_bounce_update(min_delay: float = 5.0) -> None:
    """Coalesce a cache refresh request behind a short debounce.

    A single bounce delete costs a full pagination of every bounce in
    ListMonk, so refreshing per-row (or per-click on "Delete") turns a bulk
    delete into hundreds of overlapping full-table scans. While a refresh is
    pending the request is simply absorbed; the periodic updater still
    guarantees a refresh within ``UPDATE_INTERVAL``.
    """
    global _pending_update

    if _pending_update is not None and not _pending_update.done():
        return

    async def _run():
        try:
            await asyncio.sleep(min_delay)
            await update_hard_bounce_counts()
        finally:
            global _pending_update
            _pending_update = None

    _pending_update = spawn(_run())


def get_hard_bounce_count(campaign_id: int) -> int:
    """Get cached hard bounce count for a campaign."""
    return _hard_bounce_counts.get(campaign_id, 0)


def get_all_hard_bounce_counts() -> dict[int, int]:
    """Get all cached hard bounce counts."""
    return _hard_bounce_counts.copy()


def get_last_updated() -> str:
    """Get last update timestamp."""
    return _last_updated


async def start_cache_updater():
    """Background task to keep cache updated."""
    logger.info("Starting hard bounce cache updater")

    # Wait for client to be ready before first update
    for _ in range(10):  # Wait up to 5 seconds
        if _is_client_ready():
            break
        await asyncio.sleep(0.5)

    # Initial update
    await update_hard_bounce_counts()

    while True:
        await asyncio.sleep(UPDATE_INTERVAL)
        await update_hard_bounce_counts()