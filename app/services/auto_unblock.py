"""
Auto-unblock service: Finds subscribers who clicked links or opened campaigns
but are currently blocklisted because of bounces, and re-enables them +
deletes their bounce records.

Logic: If a subscriber clicked or opened, they are a real engaged user, so a
*bounce-driven* blocklisting is a false positive. Only those are reversed:
a blocklist with no bounce record behind it was a deliberate decision (the
Leads/Replies "Blocklist" actions, a blocklist import, the unsubscribe
engine), and an engaged subscriber is exactly who replies "remove me" — the
old "anyone blocklisted who ever opened" rule re-enabled them every 6 hours.
"""

import asyncio
import logging
from datetime import datetime, timezone
from app.services.listmonk_client import ListMonkClient, listmonk as listmonk_singleton
from app.services.unsubscribe_log import load_log, _load_processed_list

logger = logging.getLogger("auto_unblock")

QUERY_BLOCKLISTED_ENGAGED = (
    "subscribers.status = 'blocklisted' "
    "AND subscribers.id IN (SELECT DISTINCT subscriber_id FROM bounces) "
    "AND (subscribers.id IN (SELECT DISTINCT subscriber_id FROM link_clicks) "
    "OR subscribers.id IN (SELECT DISTINCT subscriber_id FROM campaign_views))"
)

# Backward-compatible alias
QUERY_BLOCKLISTED_CLICKERS = QUERY_BLOCKLISTED_ENGAGED


# Bounded concurrency: unblocking a subscriber is a status update plus one
# bounce fetch and a delete per record, so serial processing made a large
# engaged-blocklisted set take minutes (and hold the manual-run request open).
UNBLOCK_CONCURRENCY = 10
BOUNCE_DELETE_CONCURRENCY = 10


def _suppressed_emails() -> set[str]:
    """Addresses that asked to be removed and must stay blocklisted.

    A subscriber the unsubscribe engine handled can also carry a bounce
    record, so the SQL bounce criterion alone does not protect them. Both the
    processed set (survives log deletion) and the log itself are consulted.
    """
    # _load_processed_list, not load_processed_emails: the latter seeds (and
    # writes) the state file on first use, which a read-only check must not.
    emails = set(_load_processed_list())
    emails.update(
        (r.get("email") or "").strip().lower()
        for r in load_log() if isinstance(r, dict)
    )
    emails.discard("")
    return emails


async def find_blocklisted_engaged(client: ListMonkClient) -> list[dict]:
    """Find bounce-blocklisted subscribers who clicked or opened any campaign.

    Unsubscribed addresses are excluded even when they bounced: an opt-out is
    never undone automatically.
    """
    subs = await client.paginate_all(
        client.get_subscribers, per_page=500, query=QUERY_BLOCKLISTED_ENGAGED,
    )
    suppressed = _suppressed_emails()
    if not suppressed:
        return subs
    kept = [s for s in subs if (s.get("email") or "").strip().lower() not in suppressed]
    if len(kept) != len(subs):
        logger.info(
            f"Auto-unblock: kept {len(subs) - len(kept)} unsubscribed subscriber(s) blocklisted"
        )
    return kept


async def find_blocklisted_clickers(client: ListMonkClient) -> list[dict]:
    """Backward-compatible alias for find_blocklisted_engaged."""
    return await find_blocklisted_engaged(client)


async def delete_bounce_records_for_subscribers(client: ListMonkClient, subscriber_ids: list[int]) -> int:
    """Delete all bounce records for the given subscriber IDs (bounded concurrency).

    Each coroutine returns its own count; results are aggregated after gather
    to avoid mutating shared state from concurrent tasks.
    """
    sem = asyncio.Semaphore(BOUNCE_DELETE_CONCURRENCY)

    async def _delete_for_subscriber(sid: int) -> int:
        count = 0
        async with sem:
            try:
                res = await client.get_subscriber_bounces(sid)
            except Exception as e:
                logger.error(f"Failed to fetch bounces for subscriber {sid}: {e}")
                return 0

            for b in res.get("data") or []:
                bid = b.get("id")
                if not bid:
                    continue
                try:
                    await client.delete_bounce(bid)
                    count += 1
                except Exception as e:
                    logger.error(f"Failed to delete bounce {bid}: {e}")
        return count

    results = await asyncio.gather(*(_delete_for_subscriber(sid) for sid in subscriber_ids))
    return sum(results)


async def delete_bounce_records(client: ListMonkClient, emails: set[str]) -> int:
    """Delete all bounce records for the given email addresses (fallback).

    Each coroutine returns its own count; results are aggregated after gather
    to avoid mutating shared state from concurrent tasks.
    """
    all_bounces = await client.paginate_all(client.get_bounces, per_page=500)
    bounce_ids_to_delete = [
        b["id"] for b in all_bounces
        if b.get("email") in emails and b.get("id")
    ]

    sem = asyncio.Semaphore(BOUNCE_DELETE_CONCURRENCY)

    async def _delete(bid: int) -> int:
        async with sem:
            try:
                await client.delete_bounce(bid)
                return 1
            except Exception as e:
                logger.error(f"Failed to delete bounce {bid}: {e}")
                return 0

    results = await asyncio.gather(*(_delete(bid) for bid in bounce_ids_to_delete))
    return sum(results)


async def unblock_subscribers(client: ListMonkClient, subscribers: list[dict]) -> dict:
    """Unblock subscribers: set status to enabled + delete their bounce records.

    Each coroutine returns its own result tuple; results are aggregated after
    gather to avoid mutating shared state from concurrent tasks.
    """
    sem = asyncio.Semaphore(UNBLOCK_CONCURRENCY)

    # Step 1: Re-enable all subscribers
    async def _unblock(s: dict) -> tuple[str, int, bool]:
        """Returns (email, subscriber_id, success)."""
        async with sem:
            try:
                await client.update_subscriber(s["id"], {
                    "email": s["email"],
                    "name": s.get("name", ""),
                    "status": "enabled",
                    "lists": [l["id"] for l in (s.get("lists") or [])],
                    "attribs": s.get("attribs", {}),
                })
                logger.info(f"Unblocked: {s['email']}")
                return (s["email"], s["id"], True)
            except Exception as e:
                logger.error(f"Failed to unblock {s['email']}: {e}")
                return (s["email"], s["id"], False)

    results = await asyncio.gather(*(_unblock(s) for s in subscribers))
    unblocked = [email for email, _, ok in results if ok]
    unblocked_ids = [sid for _, sid, ok in results if ok]
    success = sum(1 for _, _, ok in results if ok)
    failed = sum(1 for _, _, ok in results if not ok)

    # Step 2: Delete bounce records for unblocked subscribers
    if unblocked_ids:
        bounces_deleted = await delete_bounce_records_for_subscribers(client, unblocked_ids)
        logger.info(f"Deleted {bounces_deleted} bounce records")
        if bounces_deleted > 0:
            from app.services.hard_bounce_cache import schedule_hard_bounce_update
            schedule_hard_bounce_update()
    else:
        bounces_deleted = 0

    return {
        "success": success,
        "failed": failed,
        "bounces_deleted": bounces_deleted,
        "unblocked": unblocked,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def run_auto_unblock() -> dict:
    """Main entry point: find and unblock all blocklisted engaged subscribers.
    Uses the singleton client when available, falls back to creating one."""
    try:
        subs = await find_blocklisted_engaged(listmonk_singleton)
        if not subs:
            logger.info("No blocklisted engaged subscribers found")
            return {"success": 0, "failed": 0, "bounces_deleted": 0,
                    "unblocked": [], "timestamp": datetime.now(timezone.utc).isoformat()}
        logger.info(f"Found {len(subs)} blocklisted engaged subscriber(s) to unblock")
        return await unblock_subscribers(listmonk_singleton, subs)
    except RuntimeError:
        # Singleton not started (CLI usage) — create standalone client
        client = ListMonkClient()
        await client.start()
        try:
            subs = await find_blocklisted_engaged(client)
            if not subs:
                return {"success": 0, "failed": 0, "bounces_deleted": 0,
                        "unblocked": [], "timestamp": datetime.now(timezone.utc).isoformat()}
            return await unblock_subscribers(client, subs)
        finally:
            await client.close()


# Standalone CLI usage
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    result = asyncio.run(run_auto_unblock())
    print(f"\nResult: {result['success']} unblocked, {result['failed']} failed, {result['bounces_deleted']} bounces deleted")
    for email in result["unblocked"]:
        print(f"  {email}")
