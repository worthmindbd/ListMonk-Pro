import asyncio
import logging
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from app.config import settings
from app.services.imap_unsubscribe import (
    get_stats, check_imap_status, scan_and_unsubscribe,
)
from app.services.unsubscribe_log import (
    load_log, save_log, load_settings, save_settings,
    unmark_processed, _log_lock,
)
from app.services.link_unsubscribe import scan_link_unsubscribes
from app.services.listmonk_client import listmonk
from app.services.export_service import dict_list_to_csv

router = APIRouter()

# Pagination bounds. These lists are served from an in-memory JSON log, so the
# cap exists to stop a single request from materialising an unbounded slice.
MAX_PAGE = 1000
MAX_PER_PAGE = 200

# "Undo all" touches every record in the log, and each one costs 2-3 serial
# ListMonk round trips. Left unbounded a large log holds the worker for hours
# and the client times out long before anything is restored.
RESET_MAX_RECORDS = 5000
RESET_CONCURRENCY = 10


@router.get("/settings")
async def get_unsub_settings():
    """Return current unsubscribe scanner settings."""
    return load_settings()


@router.put("/settings")
async def update_unsub_settings(request: Request):
    """Update unsubscribe scanner settings (e.g., blocklist toggle)."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Settings must be a JSON object")
    if "blocklist_enabled" in data and not isinstance(data["blocklist_enabled"], bool):
        raise HTTPException(status_code=400, detail="blocklist_enabled must be a boolean")
    # Only merge keys this scanner actually owns: an arbitrary client dict
    # (including multi-megabyte junk) must not be persisted to DATA_DIR.
    allowed = set(load_settings().keys())
    save_settings({k: v for k, v in data.items() if k in allowed})
    return load_settings()


@router.get("")
async def get_unsubscribes(page: int = Query(1, ge=1, le=MAX_PAGE),
                           per_page: int = Query(25, ge=1, le=MAX_PER_PAGE)):
    """Return paginated unsubscribe records (newest first)."""
    records = load_log()
    records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    total = len(records)
    start = (page - 1) * per_page
    end = start + per_page
    return {
        "data": {
            "results": records[start:end],
            "total": total,
            "page": page,
            "per_page": per_page,
        }
    }


@router.get("/stats")
async def get_unsubscribe_stats(campaign_id: int = 0):
    """Return aggregate unsubscribe counts. Optionally filter by campaign_id."""
    stats = get_stats()
    if campaign_id:
        records = load_log()
        campaign_count = sum(1 for r in records if r.get("campaign_id") == campaign_id)
        stats["campaign_count"] = campaign_count
    return stats


@router.get("/imap-status")
async def get_imap_status():
    """Check if IMAP is configured and can connect."""
    return await check_imap_status()


@router.get("/campaigns")
async def get_campaign_groups():
    """Return unsubscribe records grouped by individual campaign (campaign_id)."""
    records = load_log()
    groups: dict = {}

    for r in records:
        cid = r.get("campaign_id") if r.get("campaign_id") is not None else "unknown"
        group_key = str(cid)
        if group_key not in groups:
            groups[group_key] = {
                "campaign_key": r.get("campaign_key", "unknown"),
                "campaign_name": r.get("campaign_name", ""),
                "campaign_id": cid,
                "group_key": group_key,
                "count": 0,
            }
        groups[group_key]["count"] += 1

    # Sort by campaign_key descending (most recent month first), then by campaign name
    sorted_groups = sorted(
        groups.values(),
        key=lambda g: (g["campaign_key"], g.get("campaign_name", "")),
        reverse=True,
    )
    return {"data": sorted_groups}


@router.get("/campaign/{campaign_id}")
async def get_campaign_records(campaign_id: int,
                               page: int = Query(1, ge=1, le=MAX_PAGE),
                               per_page: int = Query(50, ge=1, le=MAX_PER_PAGE)):
    """Return paginated records for a specific campaign by its ID."""
    records = load_log()
    campaign_records = [r for r in records if r.get("campaign_id") == campaign_id]
    campaign_records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)

    total = len(campaign_records)
    link_count = sum(1 for r in campaign_records if r.get("source") == "link")
    email_count = total - link_count
    start = (page - 1) * per_page
    end = start + per_page
    return {
        "data": {
            "results": campaign_records[start:end],
            "total": total,
            "link_count": link_count,
            "email_count": email_count,
            "page": page,
            "per_page": per_page,
            "campaign_id": campaign_id,
            "campaign_name": campaign_records[0].get("campaign_name", "") if campaign_records else "",
        }
    }


@router.delete("/campaigns")
async def delete_campaign_groups(campaign_ids: list[int] = Query(default=[])):
    """Delete every unsubscribe record belonging to the given campaigns.

    The campaign-grouped list view selects several campaign cards at once; one
    log rewrite is both faster and safer than issuing a delete per card.
    """
    wanted = set(campaign_ids)
    if not wanted:
        raise HTTPException(status_code=400, detail="No campaign ids provided")
    async with _log_lock:
        records = load_log()
        remaining = [r for r in records if r.get("campaign_id") not in wanted]
        removed = len(records) - len(remaining)
        save_log(remaining)
    return {
        "removed": removed,
        "campaigns": len(wanted),
        "message": f"Removed {removed} record(s) from {len(wanted)} campaign(s)",
    }


@router.delete("/campaign/{campaign_id}")
async def delete_campaign_group(campaign_id: int):
    """Delete all unsubscribe records for a specific campaign by its ID."""
    async with _log_lock:
        records = load_log()
        before_count = len(records)
        remaining = [r for r in records if r.get("campaign_id") != campaign_id]
        removed = before_count - len(remaining)
        save_log(remaining)
    return {"removed": removed, "message": f"Removed {removed} record(s) from campaign {campaign_id}"}


_MAX_DELETE_EMAILS = 100


@router.delete("/records")
async def delete_records(emails: list[str] = Query(default=[])):
    """Delete specific unsubscribe records by email (capped at 100)."""
    if not emails:
        raise HTTPException(status_code=400, detail="No emails provided")
    if len(emails) > _MAX_DELETE_EMAILS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many emails ({len(emails)}). Maximum is {_MAX_DELETE_EMAILS}.",
        )
    email_set = set(emails)
    async with _log_lock:
        records = load_log()
        before_count = len(records)
        remaining = [r for r in records if r.get("email") not in email_set]
        removed = before_count - len(remaining)
        save_log(remaining)
    return {"removed": removed, "message": f"Removed {removed} record(s)"}


@router.get("/campaign/{campaign_id}/export")
async def export_campaign_csv(campaign_id: int):
    """Export a single campaign's unsubscribe records as CSV."""
    records = load_log()
    campaign_records = [r for r in records if r.get("campaign_id") == campaign_id]
    if not campaign_records:
        raise HTTPException(status_code=404, detail="No records found for this campaign")

    campaign_records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    columns = ["email", "name", "keyword", "campaign_name", "campaign_key", "timestamp"]
    filename = f"unsubscribes_campaign_{campaign_id}.csv"
    return StreamingResponse(
        dict_list_to_csv(campaign_records, columns),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/export")
async def export_unsubscribes():
    """Export all unsubscribe records as CSV."""
    records = load_log()
    if not records:
        raise HTTPException(status_code=404, detail="No unsubscribe records found")

    records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    columns = ["email", "name", "campaign_name", "campaign_key", "campaign_id", "keyword", "subject", "subscriber_id", "timestamp"]
    return StreamingResponse(
        dict_list_to_csv(records, columns),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=unsubscribes_export.csv"},
    )


@router.post("/scan")
async def trigger_scan():
    """Manually trigger both IMAP and link-based unsubscribe scans."""
    try:
        imap_result = await scan_and_unsubscribe(listmonk)
        link_result = await scan_link_unsubscribes(listmonk)
    except Exception as e:
        logging.getLogger("unsubscribes").error(f"unsubscribe scan failed: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Scan failed: {e}")

    # Rule 7: a mailbox that was supposed to be scanned but wasn't must not
    # ride home on a 200 "Scan complete". The service reports both "not
    # configured" and "connection failed" with the same message, so the
    # environment decides which one it was.
    if imap_result.get("message") == "IMAP not configured or connection failed":
        if settings.imap_configured:
            raise HTTPException(
                status_code=502,
                detail=(
                    "IMAP unsubscribe scan failed (mailbox unreachable); "
                    f"link scan still processed {link_result.get('processed', 0)} record(s)"
                ),
            )
        return {
            "imap": imap_result,
            "link": link_result,
            "message": (
                "IMAP monitor not configured — link scan only: "
                f"{link_result.get('processed', 0)} unsubscribed"
            ),
        }

    # The scanner also counts per-message failures (`errors`) while still
    # returning its normal summary — a configured mailbox that hit errors is a
    # failed scan too, so it must not ride home on a clean "Scan complete".
    if settings.imap_configured and imap_result.get("errors", 0) > 0:
        raise HTTPException(
            status_code=502,
            detail=(
                f"IMAP unsubscribe scan hit {imap_result['errors']} error(s) "
                f"after processing {imap_result.get('processed', 0)} record(s); "
                f"link scan processed {link_result.get('processed', 0)} record(s)"
            ),
        )

    # The link scanner reports a failed ListMonk fetch the same way (errors
    # counter, 200 summary), so it needs the same honesty check: a broken
    # link scan must not answer "Scan complete: 0 unsubscribed".
    if link_result.get("errors", 0) > 0:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Link unsubscribe scan hit {link_result['errors']} error(s) "
                f"after processing {link_result.get('processed', 0)} record(s); "
                f"IMAP scan processed {imap_result.get('processed', 0)} record(s)"
            ),
        )

    total_processed = imap_result.get("processed", 0) + link_result.get("processed", 0)
    return {
        "imap": imap_result,
        "link": link_result,
        "message": f"Scan complete: {total_processed} unsubscribed",
    }


@router.delete("/clear")
async def clear_unsubscribes():
    """Clear all unsubscribe records to free storage."""
    async with _log_lock:
        records = load_log()
        count = len(records)
        save_log([])
    return {"cleared": count, "message": f"Removed {count} record(s)"}


@router.post("/reset")
async def reset_all_unsubscribes():
    """
    UNDO all unsubscribe actions: re-subscribe users to their original lists,
    remove blocklist status, and remove restored records from the log.
    Failed records remain in the log so they can be retried.
    """
    records = load_log()
    if not records:
        return {"message": "No records to reset", "restored": 0, "failed": 0}

    if len(records) > RESET_MAX_RECORDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Refusing to reset {len(records)} records at once "
                f"(limit {RESET_MAX_RECORDS}). Remove records per campaign first."
            ),
        )

    # Snapshot the identity of every record we intend to restore BEFORE
    # processing begins. We use (subscriber_id, email, timestamp) so a NEW
    # unsubscribe for the same subscriber that arrives during processing
    # is never accidentally removed from the log.
    restoring_identities: set[tuple] = {
        (r.get("subscriber_id"), r.get("email", "unknown"), r.get("timestamp", ""))
        for r in records
    }

    sem = asyncio.Semaphore(RESET_CONCURRENCY)
    results: list[dict] = [None] * len(records)  # type: ignore[list-item]

    async def restore(idx: int, r: dict) -> None:
        async with sem:
            sub_id = r.get("subscriber_id")
            email_addr = r.get("email", "unknown")
            lists_removed = r.get("lists_removed", [])

            if not sub_id:
                results[idx] = {
                    "ok": False,
                    "detail": f"SKIP {email_addr}: no subscriber_id",
                }
                return

            try:
                # Step 1: Fetch current subscriber data from ListMonk
                result = await listmonk.get_subscriber(sub_id)
                subscriber = result.get("data") or {}

                if not subscriber:
                    results[idx] = {
                        "ok": False,
                        "detail": f"SKIP {email_addr}: subscriber {sub_id} not found",
                    }
                    return

                # Step 2: Re-enable subscriber (remove blocklist)
                current_lists = [lst["id"] for lst in subscriber.get("lists", [])]
                await listmonk.update_subscriber(sub_id, {
                    "email": subscriber["email"],
                    "name": subscriber.get("name", ""),
                    "status": "enabled",
                    "lists": current_lists,
                    "attribs": subscriber.get("attribs", {}),
                })

                # Step 3: Re-subscribe to removed lists
                if lists_removed:
                    await listmonk.modify_list_memberships({
                        "ids": [sub_id],
                        "action": "add",
                        "target_list_ids": lists_removed,
                        "status": "confirmed",
                    })

                results[idx] = {
                    "ok": True,
                    "detail": f"OK {email_addr}: enabled + re-added to lists {lists_removed}",
                }

            except Exception as e:
                results[idx] = {
                    "ok": False,
                    "detail": f"FAIL {email_addr}: {e}",
                }

    await asyncio.gather(*(restore(i, r) for i, r in enumerate(records)))

    details: list[str] = []
    restored_count = 0
    for res in results:
        if res is None:
            continue
        details.append(res["detail"])
        if res["ok"]:
            restored_count += 1
            logging.getLogger("unsubscribes").info(res["detail"])
        else:
            logging.getLogger("unsubscribes").error(res["detail"])

    failed = len(records) - restored_count

    # Only clear records that were in our original snapshot AND were
    # successfully restored. New records that arrived during processing
    # are preserved because their timestamp won't match the snapshot.
    async with _log_lock:
        fresh_records = load_log()
        remaining_records = [
            r for r in fresh_records
            if (r.get("subscriber_id"), r.get("email", "unknown"), r.get("timestamp", ""))
            not in restoring_identities
            or not any(
                res and res["ok"]
                for res in results
            )
        ]
        # More precise: only remove records whose identity is in the snapshot
        # AND whose corresponding restore succeeded. Build a set of identities
        # that were successfully restored.
        ok_indices = {i for i, res in enumerate(results) if res and res["ok"]}
        ok_identities = {
            (records[i].get("subscriber_id"), records[i].get("email", "unknown"), records[i].get("timestamp", ""))
            for i in ok_indices
        }
        remaining_records = [
            r for r in fresh_records
            if (r.get("subscriber_id"), r.get("email", "unknown"), r.get("timestamp", ""))
            not in ok_identities
        ]
        save_log(remaining_records)

    # Allow future unsubscribes from restored subscribers to be detected again.
    # (The suppressed-email set is intentionally NOT cleared when log records
    # are merely deleted — otherwise the scanners would re-handle them.)
    restored_emails = [records[i].get("email", "") for i in ok_indices]
    if restored_emails:
        await unmark_processed(restored_emails)

    return {
        "message": f"Reset complete: {restored_count} restored, {failed} failed (kept in log for retry)",
        "restored": restored_count,
        "failed": failed,
        "details": details,
    }
