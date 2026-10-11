"""
Paginated bounce listing with opener exclusion — fast path for UI pages.
"""

from typing import Optional

from app.services.bounce_filters import (
    bounce_campaign_id,
    build_opener_emails_by_campaign,
    email_opened_campaign,
    exclude_openers_from_bounces,
    filter_bounces_excluding_openers,
)
from app.services.hard_bounce_cache import get_all_hard_bounce_counts, get_hard_bounce_count
from app.services.listmonk_client import ListMonkClient
from app.services.opener_cache import get_cached_opener_emails, is_cached

LM_FETCH_SIZE = 100
MAX_LM_PAGES = 300
CHECK_CONCURRENCY = 25

# Fetching a campaign's opener set is a single ListMonk query no matter how
# many bounces it has, so it beats the per-email fallback as soon as a batch
# holds more than a handful of bounces — and it warms opener_cache, which makes
# every later page an in-memory lookup instead of another round of queries.
COLD_CACHE_EMAIL_THRESHOLD = 20


async def filter_bounces_excluding_openers_fast(
    client: ListMonkClient,
    bounces: list[dict],
) -> list[dict]:
    """Filter a bounce batch using one lightweight query per bounce."""
    if not bounces:
        return bounces

    campaign_ids = {cid for b in bounces if (cid := bounce_campaign_id(b))}
    if not campaign_ids:
        return bounces

    if all(is_cached(cid) for cid in campaign_ids):
        opener_map = {
            cid: get_cached_opener_emails(cid) or set() for cid in campaign_ids
        }
        return exclude_openers_from_bounces(bounces, opener_map)

    # Cold cache. Per-email checks are only cheaper for a small batch; beyond
    # that the bulk fetch wins AND populates opener_cache, so the next page
    # hits the fast path above instead of re-querying for every bounce again.
    attributed = [b for b in bounces if bounce_campaign_id(b)]
    if len(attributed) <= COLD_CACHE_EMAIL_THRESHOLD:
        import asyncio

        sem = asyncio.Semaphore(CHECK_CONCURRENCY)
        keep: list[dict | None] = [None] * len(bounces)

        async def check(idx: int, bounce: dict):
            cid = bounce_campaign_id(bounce)
            email = bounce.get("email")
            if not cid or not email:
                keep[idx] = bounce
                return
            async with sem:
                opened = await email_opened_campaign(client, email, cid)
            if not opened:
                keep[idx] = bounce

        await asyncio.gather(*(check(i, b) for i, b in enumerate(bounces)))
        return [b for b in keep if b is not None]

    opener_map = await build_opener_emails_by_campaign(client, campaign_ids)
    return exclude_openers_from_bounces(bounces, opener_map)


def estimate_filtered_hard_total(campaign_id: Optional[int] = None) -> int | None:
    counts = get_all_hard_bounce_counts()
    if not counts:
        return None
    if campaign_id:
        return get_hard_bounce_count(campaign_id)
    return sum(counts.values())


async def fetch_filtered_bounces_page(
    client: ListMonkClient,
    page: int,
    per_page: int,
    bounce_type: str = "",
    campaign_id: Optional[int] = None,
    source: str = "",
) -> dict:
    """Return one page of bounces without loading the full dataset first."""
    needed = page * per_page
    collected: list[dict] = []
    lm_page = 1

    lm_total = 0
    raw_results: list[dict] = []
    while len(collected) < needed and lm_page <= MAX_LM_PAGES:
        res = await client.get_bounces(
            lm_page, LM_FETCH_SIZE, campaign_id, source, bounce_type,
        )
        data = res.get("data", {})
        lm_total = data.get("total", 0)
        raw_results = data.get("results", [])
        if not raw_results:
            break

        # If bounce_type is specified (e.g. "hard" or "soft"), filter by it; otherwise keep all
        if bounce_type and bounce_type.lower() != "all":
            batch = [b for b in raw_results if b.get("type") == bounce_type]
        else:
            batch = list(raw_results)

        if batch:
            # Opener exclusion is only relevant for hard bounces (false positives)
            if bounce_type == "soft":
                collected.extend(batch)
            else:
                hard_bounces = [b for b in batch if b.get("type") == "hard"]
                other_bounces = [b for b in batch if b.get("type") != "hard"]
                if hard_bounces:
                    filtered_hard = await filter_bounces_excluding_openers_fast(client, hard_bounces)
                    collected.extend(filtered_hard + other_bounces)
                else:
                    collected.extend(other_bounces)

        if lm_page * LM_FETCH_SIZE >= lm_total:
            break
        lm_page += 1

    start = (page - 1) * per_page
    results = collected[start:start + per_page]

    # Total should match what the user sees: hard-bounce cache is authoritative
    # for hard bounces (already excludes openers); otherwise use cached total or
    # ListMonk's total (or actual collected count if we finished all pages).
    reached_end = (lm_page * LM_FETCH_SIZE >= lm_total) or (len(collected) < needed and not raw_results)
    if reached_end:
        total = len(collected)
    else:
        total = max(lm_total, len(collected))

    if bounce_type == "hard":
        cached = estimate_filtered_hard_total(campaign_id)
        if cached is not None:
            total = max(cached, len(collected))

    return {"data": {"results": results, "total": total}}


async def fetch_all_filtered_bounces(
    client: ListMonkClient,
    bounce_type: str = "",
    campaign_id: Optional[int] = None,
    source: str = "",
) -> list[dict]:
    """Full filtered list for export/delete — uses bulk path when openers are cached."""
    all_bounces = await client.paginate_all(
        client.get_bounces, per_page=500,
        campaign_id=campaign_id, source=source,
        bounce_type=bounce_type,
    )
    if bounce_type and bounce_type.lower() != "all":
        all_bounces = [b for b in all_bounces if b.get("type") == bounce_type]
    if not all_bounces:
        return []

    # Soft bounces do not need opener exclusion
    if bounce_type == "soft":
        return all_bounces

    hard_bounces = [b for b in all_bounces if b.get("type") == "hard"]
    other_bounces = [b for b in all_bounces if b.get("type") != "hard"]
    if not hard_bounces:
        return other_bounces

    if len(hard_bounces) <= 500:
        filtered_hard = await filter_bounces_excluding_openers_fast(client, hard_bounces)
    else:
        filtered_hard = await filter_bounces_excluding_openers(client, hard_bounces)
    return filtered_hard + other_bounces
