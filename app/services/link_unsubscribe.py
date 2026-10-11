"""
Link Unsubscribe Scanner: Polls ListMonk API for subscribers who used the
direct unsubscribe link. Applies the same actions as IMAP unsubscribes:
removes from all lists and optionally blocklists. Records in the shared
unsubscribe_log.json with source="link".
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from app.services.listmonk_client import ListMonkClient
from app.services.unsubscribe_log import (
    append_log, load_settings, load_processed_emails, mark_processed,
)

logger = logging.getLogger("link_unsubscribe")

PER_PAGE = 100  # Patchable in tests
MAX_PAGES_PER_LIST = 500  # Safety cap: 50k subscribers per list
CAMPAIGN_PAGE_SIZE = 100
MAX_CAMPAIGN_PAGES = 20  # Safety cap: 2000 campaigns
CAMPAIGN_HISTORY_TTL = 900  # 15 minutes; campaign creation is rare

_link_scan_lock = asyncio.Lock()

# Full campaign history (newest first) used for attribution, plus its load time.
_campaign_history: list[dict] = []
_campaign_history_at: float = 0.0


def _pick_campaign_for_list_ids(campaigns: list, list_ids: set[int]) -> dict:
    """
    Pick the most recent campaign (created on or before now) that targets any
    of list_ids. `campaigns` must be pre-sorted DESC by created_at.
    Returns {campaign_id, campaign_name, campaign_key, matched_list_id}.
    matched_list_id is one of list_ids that the chosen campaign targets — used
    by the caller to set a representative list_id on the log record. When the
    campaign targets no known list (the API omitted `lists`), fall back to the
    lowest list id so the result is stable across runs instead of depending on
    set iteration order.
    """
    now = datetime.now(timezone.utc)
    fallback_list_id = min(list_ids) if list_ids else None
    for camp in campaigns:
        camp_list_ids = [l.get("id") for l in (camp.get("lists") or [])]
        # Intersect campaign's target lists with subscriber's unsubscribed lists.
        # If the API omits lists, fall back to time-based matching only.
        matched = None
        if camp_list_ids:
            # Iterate the subscriber's ids in a stable order rather than the
            # campaign's, so repeated runs attribute to the same list.
            for lid in sorted(x for x in list_ids if x is not None):
                if lid in camp_list_ids:
                    matched = lid
                    break
            if matched is None:
                continue

        created = camp.get("created_at", "")
        if not created:
            continue
        try:
            clean_iso = created.replace("Z", "+00:00")
            camp_date = datetime.fromisoformat(clean_iso)
            if camp_date.tzinfo is None:
                camp_date = camp_date.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            try:
                camp_date = datetime.fromisoformat(created[:10]).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
        if camp_date <= now:
            return {
                "campaign_id": camp.get("id"),
                "campaign_name": camp.get("name", ""),
                "campaign_key": f"{camp_date.year}-{camp_date.month:02d}",
                "matched_list_id": matched if matched is not None else fallback_list_id,
            }

    return {
        "campaign_id": None,
        "campaign_name": "No matching campaign",
        "campaign_key": _current_campaign_key(),
        "matched_list_id": fallback_list_id,
    }


def _current_campaign_key() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year}-{now.month:02d}"


def _campaign_dt(camp: dict) -> datetime | None:
    """Parse a campaign's ``created_at`` into an aware UTC datetime."""
    created = camp.get("created_at") or ""
    if not created:
        return None
    for candidate in (created, created[:10]):
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


async def _fetch_campaign_history(client: ListMonkClient) -> list[dict]:
    """Fetch the full campaign history, newest first.

    Cached briefly because the link scan runs hourly while campaign creation
    is rare, and every unsubscribed subscriber is attributed against this list.
    """
    global _campaign_history, _campaign_history_at

    now = time.monotonic()
    if _campaign_history and now - _campaign_history_at < CAMPAIGN_HISTORY_TTL:
        return _campaign_history

    campaigns: list[dict] = []
    page = 1
    while page <= MAX_CAMPAIGN_PAGES:
        result = await client.get_campaigns(
            page=page, per_page=CAMPAIGN_PAGE_SIZE,
            order_by="created_at", order="DESC",
        )
        results = result.get("data", {}).get("results", [])
        if not results:
            break
        campaigns.extend(results)
        page += 1
    else:
        logger.warning(
            f"[LINK] Stopped campaign history at {MAX_CAMPAIGN_PAGES} pages; "
            "older unsubscribes may be attributed to a newer campaign"
        )

    if campaigns:
        # _pick_campaign_for_list_ids relies on newest-first ordering to pick the
        # most recent match, and trusts the caller to have supplied it. Sort
        # explicitly so attribution cannot silently invert if the API's ordering
        # changes or a future caller forgets the DESC parameter.
        campaigns.sort(key=lambda c: _campaign_dt(c) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        _campaign_history = campaigns
        _campaign_history_at = time.monotonic()
    return campaigns


def invalidate_campaign_history() -> None:
    """Drop the cached campaign history (tests, or after a campaign change)."""
    global _campaign_history, _campaign_history_at
    _campaign_history = []
    _campaign_history_at = 0.0


async def scan_link_unsubscribes(client: ListMonkClient) -> dict:
    """
    Scan all ListMonk lists for link-unsubscribed subscribers and process them.
    Returns summary: {scanned_lists, new_found, processed, errors}.

    Attribution is per-subscriber, not per-list: when a subscriber appears as
    unsubscribed in multiple lists (which happens when the unsubscribe form
    cascades to all their lists), they are attributed to the most recent
    campaign targeting any of those lists — not to whichever list is scanned
    first.

    Serialized with a module lock so a manual scan and the hourly background
    scan cannot process (and log) the same subscribers twice.
    """
    if _link_scan_lock.locked():
        return {"scanned_lists": 0, "new_found": 0, "processed": 0, "errors": 0,
                "message": "Link scan already in progress"}

    async with _link_scan_lock:
        return await _scan_link_unsubscribes_impl(client)


async def _scan_link_unsubscribes_impl(client: ListMonkClient) -> dict:
    scanned_lists = 0
    errors = 0

    processed_emails = load_processed_emails()

    scan_settings = load_settings()
    blocklist_enabled = scan_settings.get("blocklist_enabled", False)

    try:
        lists_result = await client.get_lists(page=1, per_page=200, minimal=True)
        all_lists = lists_result.get("data", {}).get("results", [])
    except Exception as e:
        logger.error(f"Failed to fetch lists: {e}")
        return {"scanned_lists": 0, "new_found": 0, "processed": 0, "errors": 1,
                "message": f"Failed to fetch lists: {e}"}

    list_id_to_name = {lst.get("id"): lst.get("name", "") for lst in all_lists if lst.get("id")}

    # Phase 1: collect unique unsubscribed subscribers across all lists.
    # sub_map: subscriber_id -> {"sub": sub_obj, "unsub_list_ids": set[int]}
    sub_map: dict = {}
    for lst in all_lists:
        list_id = lst.get("id")
        if not list_id:
            continue

        scanned_lists += 1
        page = 1

        while True:
            try:
                result = await client.get_subscribers_by_list_status(
                    list_id=list_id,
                    subscription_status="unsubscribed",
                    page=page,
                    per_page=PER_PAGE,
                )
                subscribers = result.get("data", {}).get("results", [])
            except Exception as e:
                logger.error(f"Failed to fetch unsubscribed subscribers for list {list_id}: {e}")
                errors += 1
                break

            for sub in subscribers:
                sid = sub.get("id")
                if sid is None:
                    continue
                entry = sub_map.setdefault(sid, {"sub": sub, "unsub_list_ids": set()})
                entry["unsub_list_ids"].add(list_id)

            if len(subscribers) < PER_PAGE:
                break
            if page >= MAX_PAGES_PER_LIST:
                logger.warning(
                    f"[LINK] Pagination cap reached for list {list_id}; "
                    f"remaining subscribers will be picked up next scan"
                )
                break
            page += 1

    # Phase 2: fetch campaigns once for attribution. The full history is
    # walked (newest-first) rather than a single page: a subscriber who
    # unsubscribed months ago must be attributed to the campaign they
    # actually received, not to whichever recent campaign shares a list.
    try:
        campaigns = await _fetch_campaign_history(client)
    except Exception as e:
        logger.warning(f"Could not fetch campaigns: {e}")
        campaigns = []

    # Phase 3: process each unique subscriber
    new_records = []
    new_found = 0
    processed = 0

    for sid, entry in sub_map.items():
        sub = entry["sub"]
        unsub_list_ids = entry["unsub_list_ids"]
        email = (sub.get("email") or "").lower()
        if not email or email in processed_emails:
            continue

        new_found += 1
        sub_lists = [l["id"] for l in sub.get("lists", [])]

        try:
            if sub_lists:
                await client.modify_list_memberships({
                    "ids": [sid],
                    "action": "unsubscribe",
                    "target_list_ids": sub_lists,
                    "status": "unsubscribed",
                })
        except Exception as e:
            logger.error(f"Failed to unsubscribe {email} from lists: {e}")
            errors += 1
            continue  # Do not log — partial failure

        if blocklist_enabled:
            try:
                await client.blocklist_subscriber(sid)
                logger.info(f"[LINK] Blocklisted: {email}")
            except Exception as e:
                logger.error(f"Failed to blocklist {email}: {e}")
                # Partial success — still log the unsubscribe

        campaign = _pick_campaign_for_list_ids(campaigns, unsub_list_ids)
        primary_list_id = campaign.get("matched_list_id")
        list_name = list_id_to_name.get(primary_list_id, "")

        record = {
            "email": email,
            "name": sub.get("name", ""),
            "source": "link",
            "keyword": None,
            "list_id": primary_list_id,
            "campaign_id": campaign.get("campaign_id"),
            "campaign_name": campaign.get("campaign_name", "No matching campaign"),
            "campaign_key": campaign.get("campaign_key", _current_campaign_key()),
            "subscriber_id": sid,
            "lists_removed": sub_lists,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        new_records.append(record)
        processed_emails.add(email)
        processed += 1
        action = "Unsubscribed + Blocklisted" if blocklist_enabled else "Unsubscribed"
        logger.info(f"[LINK] {action}: {email} (list: {list_name})")

    if new_records:
        await append_log(new_records)
        await mark_processed(r["email"] for r in new_records)

    return {
        "scanned_lists": scanned_lists,
        "new_found": new_found,
        "processed": processed,
        "errors": errors,
        "message": f"Link scan complete: {processed} unsubscribed",
    }
