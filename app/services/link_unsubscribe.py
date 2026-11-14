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

from app.services.imap_helpers import campaign_has_sent, campaign_send_dt
from app.services.listmonk_client import ListMonkClient
from app.services.unsubscribe_log import (
    append_log, load_settings, load_processed_emails, mark_processed,
    load_link_seen, update_link_seen,
)

logger = logging.getLogger("link_unsubscribe")

PER_PAGE = 100  # Patchable in tests
MAX_PAGES_PER_LIST = 500  # Safety cap: 50k subscribers per list
LIST_PAGE_SIZE = 100
MAX_LIST_PAGES = 50  # Safety cap: 5000 lists
CAMPAIGN_PAGE_SIZE = 100
MAX_CAMPAIGN_PAGES = 20  # Safety cap: 2000 campaigns
CAMPAIGN_HISTORY_TTL = 900  # 15 minutes; campaign creation is rare

# Subscribers with a bounce record. Used when ListMonk's own bounce rules
# "unsubscribe" bounced addresses: those subscriptions are marked unsubscribed
# exactly like a link click, and the scanner used to log them as link
# unsubscribes, cascade them over every list and (with the toggle on)
# blocklist them.
QUERY_NOT_BOUNCED = "subscribers.id NOT IN (SELECT subscriber_id FROM bounces)"


async def _bounce_rules_unsubscribe(client: ListMonkClient) -> bool:
    """Do ListMonk's bounce actions unsubscribe anyone?

    Unreadable settings count as yes: skipping a bounced subscriber for one
    scan only delays a log entry, while treating a bounce as an opt-out can
    blocklist a live contact.
    """
    try:
        res = await client.get_settings()
        data = res.get("data") if isinstance(res, dict) else None
    except Exception as e:
        logger.warning(f"[LINK] ListMonk settings unreadable, ignoring bounced subscribers: {e}")
        return True
    if not isinstance(data, dict):
        return True
    actions = data.get("bounce.actions")
    if not isinstance(actions, dict):
        return False
    return any(
        isinstance(rule, dict) and rule.get("action") == "unsubscribe"
        for rule in actions.values()
    )

# Remembered subscribers the scan did not meet (no unsubscribed list left)
# are looked up by id, this many per ListMonk query.
SEEN_LOOKUP_CHUNK = 500  # ~3.5 KB of ids per URL


async def _resubscribed_lists(client: ListMonkClient, sids: list[int]) -> dict[int, set[int]]:
    """Subscriber id -> list ids subscribed (not ``unsubscribed``) right now.

    Only non-blocklisted subscribers with at least one active subscription
    come back, so the answer stays small. A failed lookup only postpones the
    correction to the next scan.
    """
    out: dict[int, set[int]] = {}
    for i in range(0, len(sids), SEEN_LOOKUP_CHUNK):
        chunk = [sid for sid in sids[i:i + SEEN_LOOKUP_CHUNK] if isinstance(sid, int)]
        if not chunk:
            continue
        query = (
            f"subscribers.id IN ({','.join(str(sid) for sid in chunk)}) "
            "AND subscribers.status != 'blocklisted' AND EXISTS (SELECT 1 FROM "
            "subscriber_lists sl WHERE sl.subscriber_id = subscribers.id "
            "AND sl.status != 'unsubscribed')"
        )
        try:
            res = await client.get_subscribers(1, len(chunk), query)
        except Exception as e:
            logger.warning(f"[LINK] Could not check remembered subscribers: {e}")
            continue
        data = res.get("data") if isinstance(res, dict) else None
        rows = data.get("results") if isinstance(data, dict) else None
        for sub in rows if isinstance(rows, list) else []:
            if not isinstance(sub, dict) or not isinstance(sub.get("id"), int):
                continue
            out[sub["id"]] = {
                l["id"] for l in sub.get("lists") or []
                if isinstance(l, dict) and isinstance(l.get("id"), int)
                and (l.get("subscription_status") or "") != "unsubscribed"
            }
    return out


_link_scan_lock = asyncio.Lock()
_link_scan_running = False

# Full campaign history (newest first) used for attribution, plus its load time.
_campaign_history: list[dict] = []
_campaign_history_at: float = 0.0


async def _fetch_all_lists(client: ListMonkClient) -> list[dict]:
    """Fetch every list, tolerating ListMonk's ``minimal`` shortcut.

    With ``minimal=true`` current ListMonk ignores pagination and returns the
    whole set on page 1, while older versions paginate. De-duplicating by list
    ID and stopping as soon as a page adds nothing new handles both shapes, so
    an instance with more lists than one page never silently drops the extras.
    """
    seen: set = set()
    lists: list[dict] = []

    for page in range(1, MAX_LIST_PAGES + 1):
        res = await client.get_lists(page=page, per_page=LIST_PAGE_SIZE, minimal=True)
        results = (res.get("data") or {}).get("results") or []
        if not results:
            break

        fresh = [lst for lst in results if lst.get("id") and lst["id"] not in seen]
        if not fresh:
            break

        for lst in fresh:
            seen.add(lst["id"])
        lists.extend(fresh)
    else:
        logger.warning(
            f"[LINK] List pagination stopped at {MAX_LIST_PAGES} pages; "
            "unsubscribes in the remaining lists will be missed"
        )

    return lists


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
    best, best_date = None, None
    for camp in campaigns:
        # A draft / scheduled campaign carries no unsubscribe link anyone could
        # have clicked: creating one for a list used to collect every later
        # unsubscribe from that list.
        if not campaign_has_sent(camp):
            continue
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

        # When it started sending (not when it was drafted): the newest
        # campaign the subscriber actually received wins.
        camp_date = campaign_send_dt(camp)
        if camp_date is None or camp_date > now:
            continue
        if best_date is None or camp_date > best_date:
            best_date = camp_date
            best = {
                "campaign_id": camp.get("id"),
                "campaign_name": camp.get("name", ""),
                "campaign_key": f"{camp_date.year}-{camp_date.month:02d}",
                "matched_list_id": matched if matched is not None else fallback_list_id,
            }

    if best is not None:
        return best
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
        results = (result.get("data") or {}).get("results") or []
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
    global _link_scan_running
    if _link_scan_running:
        return {"scanned_lists": 0, "new_found": 0, "processed": 0, "errors": 0,
                "message": "Link scan already in progress"}

    async with _link_scan_lock:
        _link_scan_running = True
        try:
            return await _scan_link_unsubscribes_impl(client)
        finally:
            _link_scan_running = False


async def _scan_link_unsubscribes_impl(client: ListMonkClient) -> dict:
    scanned_lists = 0
    errors = 0

    processed_emails = load_processed_emails()

    scan_settings = load_settings()
    blocklist_enabled = scan_settings.get("blocklist_enabled", False)

    try:
        all_lists = await _fetch_all_lists(client)
    except Exception as e:
        logger.error(f"Failed to fetch lists: {e}")
        return {"scanned_lists": 0, "new_found": 0, "processed": 0, "errors": 1,
                "message": f"Failed to fetch lists: {e}"}

    list_id_to_name = {lst.get("id"): lst.get("name", "") for lst in all_lists if lst.get("id")}
    subscriber_query = QUERY_NOT_BOUNCED if await _bounce_rules_unsubscribe(client) else ""

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
                    query=subscriber_query,
                )
                subscribers = (result.get("data") or {}).get("results") or []
            except Exception as e:
                logger.error(f"Failed to fetch unsubscribed subscribers for list {list_id}: {e}")
                errors += 1
                break

            for sub in subscribers:
                sid = sub.get("id")
                if sid is None:
                    continue
                # ListMonk marks every subscription of a blocklisted subscriber
                # "unsubscribed", so a bounce / manual / cleanup blocklisting
                # looked exactly like a link unsubscribe: it was logged as one
                # (inflating per-campaign counts) and "Undo All" then
                # re-enabled it. A blocklisted address gets no mail anyway, so
                # there is nothing to cascade.
                if sub.get("status") == "blocklisted":
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

    # Phase 3: process each unique subscriber. A subscriber is acted on only
    # for lists that became unsubscribed since the scanner last saw them
    # (LINK_SEEN_FILE): old unsubscribed rows of a re-imported contact are not
    # a new click, and lists this app unsubscribed through its own API are
    # pre-registered there.
    new_records = []
    new_found = 0
    processed = 0
    link_seen = load_link_seen()
    seen_updates: dict[int, set] = {}
    seen_forget: dict[int, set] = {}

    for sid, entry in sub_map.items():
        sub = entry["sub"]
        unsub_list_ids = entry["unsub_list_ids"]
        email = (sub.get("email") or "").lower()
        if not email:
            continue
        sub_list_objs = [l for l in (sub.get("lists") or []) if isinstance(l, dict) and l.get("id")]
        sub_lists = [l["id"] for l in sub_list_objs]
        active_lists = [
            l["id"] for l in sub_list_objs
            if (l.get("subscription_status") or "") != "unsubscribed"
        ]
        seen = link_seen.get(sid)
        if seen is not None:
            # A remembered list that is subscribed again (re-import, a manual
            # re-add in ListMonk) is no longer handled: the next click on it
            # is a new unsubscribe, which the memory used to hide for good.
            resubscribed = seen & set(active_lists)
            if resubscribed:
                seen_forget[sid] = resubscribed
                seen = seen - resubscribed
        if seen is None and email in processed_emails:
            # Handled before this memory existed (or by the IMAP scanner):
            # what is unsubscribed now is the baseline, not a new click.
            seen_updates[sid] = set(unsub_list_ids)
            continue
        fresh_lists = set(unsub_list_ids) - (seen or set())
        if not fresh_lists:
            continue

        new_found += 1
        # Lists still active get the cascade. Undo restores those plus the
        # lists this click unsubscribed, never ones the person left earlier.
        lists_removed = [lid for lid in sub_lists if lid in active_lists or lid in fresh_lists]

        try:
            if active_lists:
                await client.modify_list_memberships({
                    "ids": [sid],
                    "action": "unsubscribe",
                    "target_list_ids": active_lists,
                    "status": "unsubscribed",
                })
        except Exception as e:
            logger.error(f"Failed to unsubscribe {email} from lists: {e}")
            errors += 1
            continue  # Do not log — partial failure

        blocklisted_now = False
        if blocklist_enabled:
            try:
                await client.blocklist_subscriber(sid)
                blocklisted_now = True
                logger.info(f"[LINK] Blocklisted: {email}")
            except Exception as e:
                logger.error(f"Failed to blocklist {email}: {e}")
                # Partial success — still log the unsubscribe

        campaign = _pick_campaign_for_list_ids(campaigns, fresh_lists)
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
            "lists_removed": lists_removed,
            # Whether THIS engine blocklisted the subscriber: "Undo All" only
            # lifts a blocklisting it caused.
            "blocklisted": blocklisted_now,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        new_records.append(record)
        processed_emails.add(email)
        # Every list is unsubscribed now: all of them are "seen".
        seen_updates[sid] = set(sub_lists) | set(unsub_list_ids)
        processed += 1
        action = "Unsubscribed + Blocklisted" if blocklist_enabled else "Unsubscribed"
        logger.info(f"[LINK] {action}: {email} (list: {list_name})")

    if new_records:
        await append_log(new_records)
        await mark_processed(r["email"] for r in new_records)
    # Remembered subscribers this scan did not meet have no unsubscribed list
    # left (or are blocklisted / bounced): those re-subscribed to a remembered
    # list are found by id, so their next click on it counts.
    absent = [sid for sid, lists in link_seen.items() if lists and sid not in sub_map]
    if absent:
        for sid, active in (await _resubscribed_lists(client, absent)).items():
            resubscribed = link_seen.get(sid, set()) & active
            if resubscribed:
                seen_forget[sid] = resubscribed

    if seen_updates or seen_forget:
        await update_link_seen(add=seen_updates, remove=seen_forget)

    return {
        "scanned_lists": scanned_lists,
        "new_found": new_found,
        "processed": processed,
        "errors": errors,
        "message": f"Link scan complete: {processed} unsubscribed",
    }
