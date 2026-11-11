"""
Bounce filtering: subscribers who opened a campaign cannot have bounced it.

Used at ingestion (skip false positives), display/export, and bounce counts.
"""

import asyncio
import time

from app.services.listmonk_client import ListMonkClient
from app.services.opener_cache import (
    get_cached_opener_emails, get_opener_emails, get_opener_emails_for_campaigns,
)
from app.services.imap_helpers import safe_email_for_query


def campaign_views_query(campaign_id: int) -> str:
    return (
        f"subscribers.id IN (SELECT subscriber_id FROM campaign_views "
        f"WHERE campaign_id={campaign_id})"
    )


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def bounce_campaign_id(bounce: dict) -> int | None:
    # ListMonk returns "campaign": null for subscriber-initiated bounces, so
    # the key can be present with a None value — .get("campaign", {}) would
    # hand back None and blow up on the chained .get().
    campaign = bounce.get("campaign") or {}
    cid = campaign.get("id")
    return int(cid) if cid else None


def bounce_campaign_name(bounce: dict) -> str:
    """Return the attributed campaign name, or "" when unattributed."""
    campaign = bounce.get("campaign") or {}
    return campaign.get("name") or ""


async def get_campaign_opener_emails(
    client: ListMonkClient, campaign_id: int,
) -> set[str]:
    """Return lowercased emails of subscribers who opened the campaign."""
    return await get_opener_emails(client, campaign_id)


async def subscriber_opened_campaign(
    client: ListMonkClient, subscriber_id: int, campaign_id: int,
) -> bool:
    """True if the subscriber has a campaign_views row for this campaign."""
    query = (
        f"subscribers.id = {subscriber_id} "
        f"AND subscribers.id IN (SELECT subscriber_id FROM campaign_views "
        f"WHERE campaign_id={campaign_id})"
    )
    result = await client.get_subscribers(1, 1, query)
    return ((result.get("data") or {}).get("total") or 0) > 0


async def email_opened_campaign(
    client: ListMonkClient, email: str, campaign_id: int,
) -> bool:
    """True if this email opened the campaign (single ListMonk query)."""
    safe = safe_email_for_query(email)
    if not safe:
        return False
    query = (
        f"subscribers.email = '{safe}' "
        f"AND subscribers.id IN (SELECT subscriber_id FROM campaign_views "
        f"WHERE campaign_id={campaign_id})"
    )
    result = await client.get_subscribers(1, 1, query)
    return ((result.get("data") or {}).get("total") or 0) > 0


async def build_opener_emails_by_campaign(
    client: ListMonkClient, campaign_ids: set[int],
) -> dict[int, set[str]]:
    return await get_opener_emails_for_campaigns(client, campaign_ids)


def exclude_openers_from_bounces(
    bounces: list[dict],
    opener_emails_by_campaign: dict[int, set[str]],
) -> list[dict]:
    """Drop bounces whose email opened the attributed campaign."""
    filtered: list[dict] = []
    for b in bounces:
        cid = bounce_campaign_id(b)
        email = _normalize_email(b.get("email", ""))
        if cid and email and email in opener_emails_by_campaign.get(cid, set()):
            continue
        filtered.append(b)
    return filtered


# Openers *among a campaign's bounced subscribers* — all the background jobs
# need. They used to pull each campaign's complete opener list (every opener,
# full subscriber objects, 500 a page) and the 5-minute hard-bounce refresh
# repeated that whenever the 10-minute opener cache expired: a constant heavy
# load on ListMonk for an answer that concerns a handful of addresses.
BOUNCED_OPENERS_TTL = 60.0
_bounced_openers: dict[int, tuple[float, set[str]]] = {}


def invalidate_bounced_openers() -> None:
    """Drop the short-lived bounced-opener sets (tests; AGENTS.md rule 11)."""
    _bounced_openers.clear()


async def _bounced_opener_emails(client: ListMonkClient, campaign_id: int) -> set[str]:
    full = get_cached_opener_emails(campaign_id)
    if full is not None:
        return full  # the Bounces page already warmed the complete set
    now = time.monotonic()
    hit = _bounced_openers.get(campaign_id)
    if hit and now - hit[0] < BOUNCED_OPENERS_TTL:
        return hit[1]
    query = (
        f"{campaign_views_query(campaign_id)} AND subscribers.id IN "
        f"(SELECT subscriber_id FROM bounces WHERE campaign_id={campaign_id})"
    )
    subs = await client.paginate_all(client.get_subscribers, per_page=500, query=query)
    emails = {_normalize_email(s["email"]) for s in subs if s.get("email")}
    for cid in [c for c, (at, _) in _bounced_openers.items() if now - at >= BOUNCED_OPENERS_TTL]:
        _bounced_openers.pop(cid, None)
    _bounced_openers[campaign_id] = (time.monotonic(), emails)
    return emails


async def filter_bounces_excluding_openers(
    client: ListMonkClient,
    bounces: list[dict],
) -> list[dict]:
    """Exclude opener false-positives across one or many campaigns.

    Used by the background jobs (hard-bounce counts, email cleanup); costs one
    small query per campaign (see ``_bounced_opener_emails``).
    """
    campaign_ids = {cid for b in bounces if (cid := bounce_campaign_id(b))}
    if not campaign_ids:
        return bounces
    sem = asyncio.Semaphore(10)

    async def fetch(cid: int) -> tuple[int, set[str]]:
        async with sem:
            return cid, await _bounced_opener_emails(client, cid)

    opener_map = dict(await asyncio.gather(*(fetch(cid) for cid in campaign_ids)))
    return exclude_openers_from_bounces(bounces, opener_map)


async def filter_campaign_hard_bounces(
    client: ListMonkClient,
    campaign_id: int,
    bounces: list[dict],
) -> list[dict]:
    """Hard bounces for one campaign, excluding subscribers who opened it."""
    from app.services.bounce_list import filter_bounces_excluding_openers_fast

    hard = [b for b in bounces if b.get("type") == "hard"]
    return await filter_bounces_excluding_openers_fast(client, hard)
