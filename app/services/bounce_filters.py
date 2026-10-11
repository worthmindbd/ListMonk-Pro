"""
Bounce filtering: subscribers who opened a campaign cannot have bounced it.

Used at ingestion (skip false positives), display/export, and bounce counts.
"""

from app.services.listmonk_client import ListMonkClient
from app.services.opener_cache import get_opener_emails, get_opener_emails_for_campaigns
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
    return result.get("data", {}).get("total", 0) > 0


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
    return result.get("data", {}).get("total", 0) > 0


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


async def filter_bounces_excluding_openers(
    client: ListMonkClient,
    bounces: list[dict],
) -> list[dict]:
    """Exclude opener false-positives across one or many campaigns."""
    campaign_ids = {cid for b in bounces if (cid := bounce_campaign_id(b))}
    if not campaign_ids:
        return bounces
    opener_map = await build_opener_emails_by_campaign(client, campaign_ids)
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
