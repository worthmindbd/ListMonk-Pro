"""
IMAP Unsubscribe Monitor: Scans an IMAP inbox for reply emails containing
unsubscribe keywords. When found, automatically unsubscribes the sender
from all ListMonk lists and blocklists them.

Storage: unsubscribe_log.json (same pattern as schedule.json)
"""

import asyncio
import imaplib
import email
import email.policy
import email.utils
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import settings
from app.services.listmonk_client import ListMonkClient
from app.services.unsubscribe_log import (
    load_log, save_log, append_log, load_settings, save_settings,
    load_processed_emails, mark_processed,
    _log_lock,
)
from app.services.imap_helpers import safe_email_for_query, imap_date, extract_email_body

logger = logging.getLogger("imap_unsubscribe")

# Safety cap when walking campaign history backwards for re-attribution.
MAX_CAMPAIGN_PAGES = 20  # 2000 campaigns
CAMPAIGN_PAGE_SIZE = 100

UNSUBSCRIBE_KEYWORDS = [
    "remove me",
    "unsubscribe me",
    "exclude me",
]

KEYWORD_PATTERN = re.compile(
    "|".join(re.escape(kw) for kw in UNSUBSCRIBE_KEYWORDS),
    re.IGNORECASE,
)


def _extract_body(msg: email.message.EmailMessage) -> str:
    return extract_email_body(msg)


def _extract_reply_only(full_body: str) -> str:
    """
    Extract ONLY the user's reply text, stripping all quoted/forwarded
    content from the email body.

    This prevents false-positive keyword matches from our own email
    template text (e.g., footer saying "Reply with 'Remove me'").

    Handles common quote patterns:
    - Lines starting with ">" (standard quoting)
    - "On <date> <someone> wrote:" markers
    - "From: <address>" forwarding headers
    - "-----Original Message-----" (Outlook)
    - "Sent: " / "To: " / "Subject: " header blocks in quoted replies
    """
    lines = full_body.splitlines()
    reply_lines = []

    # Patterns that indicate the start of quoted/forwarded content
    quote_start_patterns = [
        # "On Mon, Jan 1, 2026 at 12:00 PM John Doe <john@example.com> wrote:"
        re.compile(r'^On\s+.+wrote:\s*$', re.IGNORECASE),
        # "-----Original Message-----"
        re.compile(r'^-{2,}\s*Original Message\s*-{2,}', re.IGNORECASE),
        # "From: Name <email>" or "From: email" at start of line (forwarded header)
        re.compile(r'^From:\s+.+@.+', re.IGNORECASE),
        # "Sent: 3/7/26 12:17 AM" (Outlook-style quoted header)
        re.compile(r'^Sent:\s+\d', re.IGNORECASE),
        # "________" or "========" separator lines (common in some clients)
        re.compile(r'^[_=]{5,}\s*$'),
        # Gmail-style: "> " quoted lines (3+ consecutive = definitely quoted block)
        # We handle ">" lines individually below
    ]

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Skip empty lines at the very beginning
        if not reply_lines and not stripped:
            continue

        # Check if this line starts a quoted section
        is_quote_start = False
        for pattern in quote_start_patterns:
            if pattern.match(stripped):
                is_quote_start = True
                break

        if is_quote_start:
            # Everything from here is quoted content — stop collecting
            break

        # Lines starting with ">" are quoted text — skip them
        if stripped.startswith('>'):
            # If we haven't collected any reply yet, keep looking
            # If we already have reply text and hit ">", the reply is above
            if reply_lines:
                break
            continue

        reply_lines.append(line)

    reply_text = '\n'.join(reply_lines).strip()

    # If we couldn't extract a reply (e.g., entire body is quoted),
    # return empty string so no false match occurs
    return reply_text


def _extract_sender_email(msg: email.message.EmailMessage) -> Optional[str]:
    """Extract the sender's email address from the From header."""
    from_header = msg.get("From", "")
    match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", from_header)
    return match.group(0).lower() if match else None


def _clean_subject(subject: str) -> str:
    """Strip Re:/Fwd:/FW: prefixes and extra whitespace."""
    cleaned = re.sub(r'^(Re|Fwd|FW|Fw)\s*:\s*', '', subject, flags=re.IGNORECASE).strip()
    # Recursively strip if multiple prefixes
    if re.match(r'^(Re|Fwd|FW|Fw)\s*:', cleaned, re.IGNORECASE):
        return _clean_subject(cleaned)
    return cleaned


def _parse_campaign_dt(created: str) -> Optional[datetime]:
    """Parse a ListMonk ``created_at`` string into an aware UTC datetime."""
    if not created:
        return None
    try:
        camp_date = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if camp_date.tzinfo is None:
            camp_date = camp_date.replace(tzinfo=timezone.utc)
        return camp_date
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(created[:10]).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None


def _match_campaign(
    campaigns: list[dict],
    email_date: Optional[datetime] = None,
    sub_list_ids: Optional[set] = None,
) -> Optional[dict]:
    """
    Match a reply email to the most recent ListMonk campaign created on or
    before the email date whose target lists intersect the subscriber's lists.

    If `sub_list_ids` is provided, only campaigns whose `lists` include at
    least one of those IDs are considered — this prevents attributing a reply
    to a campaign the subscriber never received. If a campaign has no `lists`
    field populated (API sometimes omits it), fall back to date-only matching
    for that campaign, mirroring link_unsubscribe._pick_campaign_for_list_ids.

    Returns {campaign_id, campaign_name, campaign_subject, matched_list_id}
    or None if nothing qualifies.
    """
    if not email_date:
        email_date = datetime.now(timezone.utc)
    if not campaigns:
        return None

    best_match = None
    best_date = None

    for camp in campaigns:
        camp_date = _parse_campaign_dt(camp.get("created_at", ""))
        if camp_date is None:
            continue

        if camp_date > email_date:
            continue

        camp_list_ids = [l.get("id") for l in (camp.get("lists") or [])]
        matched_list_id = None
        if sub_list_ids and camp_list_ids:
            for lid in camp_list_ids:
                if lid in sub_list_ids:
                    matched_list_id = lid
                    break
            if matched_list_id is None:
                continue

        if best_date is None or camp_date > best_date:
            best_date = camp_date
            best_match = {
                "campaign_id": camp.get("id"),
                "campaign_name": camp.get("name", ""),
                "campaign_subject": camp.get("subject", ""),
                "matched_list_id": matched_list_id,
            }

    return best_match


def connect_imap() -> Optional[imaplib.IMAP4_SSL | imaplib.IMAP4]:
    """Connect to the IMAP server. Returns None on failure."""
    if not settings.imap_configured:
        logger.warning("IMAP not configured, skipping scan")
        return None
    try:
        if settings.imap_use_ssl:
            conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
        else:
            conn = imaplib.IMAP4(settings.imap_host, settings.imap_port)
        conn.login(settings.imap_user, settings.imap_pass)
        return conn
    except Exception as e:
        logger.error(f"IMAP connection failed: {e}")
        return None


async def check_imap_status() -> dict:
    """Check IMAP connection status without scanning (off the event loop)."""
    if not settings.imap_configured:
        return {"configured": False, "connected": False, "error": "IMAP not configured in .env"}
    try:
        conn = await asyncio.to_thread(connect_imap)
        if conn:
            await asyncio.to_thread(conn.logout)
            return {"configured": True, "connected": True, "error": None}
        return {"configured": True, "connected": False, "error": "Connection failed"}
    except Exception as e:
        return {"configured": True, "connected": False, "error": str(e)}


_scan_lock = asyncio.Lock()


def _prune_existing_records(records: list[dict], valid_cids: set) -> list[dict]:
    """Drop records attributed to campaigns that no longer exist in ListMonk.

    Records without a campaign_id (e.g. "No matching campaign" link records)
    are kept — a missing attribution is not evidence of a deleted campaign.
    Callers must only invoke this with a *complete* campaign id set.
    """
    return [
        r for r in records
        if r.get("campaign_id") is None or r.get("campaign_id") in valid_cids
    ]


def _oldest_pending_record_dt(records: list[dict]) -> Optional[datetime]:
    """Oldest timestamp among email-source records still awaiting attribution."""
    oldest = None
    for r in records:
        if r.get("source") != "email" or r.get("reattributed_at"):
            continue
        if not r.get("lists_removed"):
            continue
        ts = r.get("timestamp", "")
        try:
            rec_date = datetime.fromisoformat(ts) if ts else datetime.now(timezone.utc)
        except (ValueError, TypeError):
            continue
        if rec_date.tzinfo is None:
            rec_date = rec_date.replace(tzinfo=timezone.utc)
        else:
            rec_date = rec_date.astimezone(timezone.utc)
        if oldest is None or rec_date < oldest:
            oldest = rec_date
    return oldest


async def _fetch_campaign_history(client: ListMonkClient, cutoff: Optional[datetime]) -> tuple[list[dict], bool, bool]:
    """Fetch campaigns newest-first, walking back until ``cutoff`` is covered.

    Returns ``(campaigns, complete_all, covers_cutoff)``:

    * ``complete_all`` — every campaign in ListMonk was fetched. Only then is
      it safe to prune records whose campaign no longer exists.
    * ``covers_cutoff`` — the fetched window provably contains every campaign
      that could match a record at or after ``cutoff``. Re-attribution against
      a truncated newest-first list would silently reassign old records to
      whichever recent campaign happens to share a list, so it must be False
      whenever we stopped early. When ``cutoff`` is None there is no pending
      re-attribution work, so a single page is both cheap and safe.
    """
    if cutoff is None:
        # Steady state: nothing left to re-attribute, so one page is enough
        # for matching new replies. complete_all still lets pruning run for
        # the common case of a campaign count that fits on one page.
        try:
            result = await client.get_campaigns(
                page=1, per_page=CAMPAIGN_PAGE_SIZE,
                order_by="created_at", order="DESC",
            )
        except Exception as e:
            logger.error(f"[IMAP] Failed to fetch campaigns: {e}")
            return [], False, False
        data = result.get("data", {})
        results = data.get("results", [])
        complete = results and data.get("total", 0) <= len(results)
        return results, bool(complete), True

    campaigns: list[dict] = []
    page = 1
    while True:
        try:
            result = await client.get_campaigns(
                page=page, per_page=CAMPAIGN_PAGE_SIZE,
                order_by="created_at", order="DESC",
            )
        except Exception as e:
            logger.error(f"[IMAP] Failed to fetch campaigns: {e}")
            return campaigns, False, False

        data = result.get("data", {})
        results = data.get("results", [])
        if not results:
            return campaigns, True, True  # reached the end of history
        campaigns.extend(results)

        oldest_seen = None
        for c in campaigns:
            dt = _parse_campaign_dt(c.get("created_at", ""))
            if dt is not None and (oldest_seen is None or dt < oldest_seen):
                oldest_seen = dt
        covers_cutoff = oldest_seen is not None and oldest_seen <= cutoff

        if page >= MAX_CAMPAIGN_PAGES:
            if not covers_cutoff:
                logger.warning(
                    f"[IMAP] Stopped campaign history at {MAX_CAMPAIGN_PAGES} pages "
                    "without covering the oldest pending record; skipping "
                    "re-attribution to avoid mis-assigning it"
                )
            return campaigns, False, covers_cutoff
        page += 1


def _reattribute_existing_records(
    records: list[dict], campaigns_list: list[dict], allow_removal: bool = True
) -> tuple[int, int]:
    """
    One-time backfill: re-run list-aware attribution on existing email-source
    records that were stored with the old date-only matcher.

    - If a record matches a different campaign, update it in place.
    - If a record matches no campaign, mark it for removal (caller filters)
      — only when `allow_removal` is set, i.e. the campaign list is complete.
    - Skips records already marked with `reattributed_at`, records without
      `lists_removed`, and non-email records.

    Returns (changed_count, removed_count).
    """
    changed = 0
    removed = 0
    for r in records:
        if r.get("source") != "email":
            continue
        if r.get("reattributed_at"):
            continue
        lists_removed = r.get("lists_removed") or []
        if not lists_removed:
            continue

        ts = r.get("timestamp", "")
        try:
            rec_date = datetime.fromisoformat(ts) if ts else datetime.now(timezone.utc)
            if rec_date.tzinfo is None:
                rec_date = rec_date.replace(tzinfo=timezone.utc)
            else:
                rec_date = rec_date.astimezone(timezone.utc)
        except (ValueError, TypeError):
            rec_date = datetime.now(timezone.utc)

        new_match = _match_campaign(
            campaigns_list, rec_date, set(lists_removed)
        )
        if new_match:
            new_cid = new_match["campaign_id"]
            if r.get("campaign_id") != new_cid:
                r["campaign_id"] = new_cid
                r["campaign_name"] = new_match["campaign_name"]
                r["matched_list_id"] = new_match.get("matched_list_id")
                changed += 1
            r["reattributed_at"] = datetime.now(timezone.utc).isoformat()
        elif allow_removal:
            r["_remove"] = True
            removed += 1

    return changed, removed


async def scan_and_unsubscribe(client: ListMonkClient) -> dict:
    """
    Scan IMAP inbox for unsubscribe requests and process them.
    Returns summary of actions taken.
    """
    if _scan_lock.locked():
        return {"scanned": 0, "matched": 0, "processed": 0, "errors": 0,
                "message": "Scan already in progress"}

    async with _scan_lock:
        conn = await asyncio.to_thread(connect_imap)
        if not conn:
            return {"scanned": 0, "matched": 0, "processed": 0, "errors": 0,
                    "message": "IMAP not configured or connection failed"}

        processed = 0
        matched = 0
        errors = 0
        scanned = 0
        new_records = []
        seen_skips: list = []
        seen_after_persist: list = []

        try:
            # Campaign history, newest first. One page is enough to match
            # *new* replies; older pages are only needed to re-attribute
            # records written before list-aware matching existed.
            try:
                campaigns_list, campaigns_complete, covers_cutoff = (
                    await _fetch_campaign_history(
                        client, _oldest_pending_record_dt(load_log())
                    )
                )
                logger.info(
                    f"[IMAP] Fetched {len(campaigns_list)} campaigns for matching "
                    f"(complete={campaigns_complete}, covers_cutoff={covers_cutoff})"
                )
            except Exception as e:
                logger.error(f"[IMAP] ERROR fetching campaigns: {e}")
                campaigns_list = []
                campaigns_complete = False
                covers_cutoff = False

            # Backfill + prune must be atomic with respect to other writers
            # (link scanner, reset/delete endpoints) so no records are lost.
            async with _log_lock:
                # Load log once and reuse across backfill, pruning, and dedup.
                existing_log = load_log()

                if campaigns_list and covers_cutoff:
                    try:
                        changed, removed = _reattribute_existing_records(
                            existing_log, campaigns_list,
                            allow_removal=campaigns_complete,
                        )
                        if changed or removed:
                            existing_log = [r for r in existing_log if not r.get("_remove")]
                            save_log(existing_log)
                            logger.info(
                                f"[IMAP] Backfill: re-attributed {changed}, "
                                f"removed {removed} unattributable records"
                            )
                    except Exception as e:
                        logger.warning(f"[IMAP] Backfill reattribution failed (non-fatal): {e}")

                # Prune records whose campaign no longer exists in ListMonk.
                # Requires a *complete* campaign list, and never drops records
                # without a campaign_id ("No matching campaign").
                if campaigns_list and campaigns_complete:
                    try:
                        valid_cids = {c.get("id") for c in campaigns_list}
                        pruned = _prune_existing_records(existing_log, valid_cids)
                        removed_count = len(existing_log) - len(pruned)
                        if removed_count:
                            existing_log = pruned
                            save_log(existing_log)
                            logger.info(f"[IMAP] Pruned {removed_count} records for deleted campaigns")
                    except Exception as e:
                        logger.warning(f"[IMAP] Campaign prune failed (non-fatal): {e}")

                # Dedup sources (message ids from the log, emails from the
                # persistent processed set that survives log deletion/clearing).
                processed_msg_ids = {
                    r.get("message_id") for r in existing_log if r.get("message_id")
                }

            processed_emails_set = load_processed_emails()

            # Determine the latest campaign's creation date to filter emails
            latest_campaign_date = None
            for camp in campaigns_list:
                created = camp.get("created_at", "")
                if created:
                    try:
                        latest_campaign_date = datetime.fromisoformat(created[:10]).replace(tzinfo=timezone.utc)
                        break  # campaigns are sorted DESC, first one is latest
                    except (ValueError, TypeError):
                        continue

            await asyncio.to_thread(conn.select, "INBOX")

            # Only fetch UNSEEN emails from the campaign month. Processed
            # messages are marked \Seen, so clearing/resetting the log no
            # longer causes old unsubscribe replies to be handled again.
            if latest_campaign_date:
                # Search from the 1st of the campaign month
                since_date = latest_campaign_date.replace(day=1)
                since_str = imap_date(since_date)
                status, msg_ids = await asyncio.to_thread(
                    conn.search, None, f'(UNSEEN SINCE {since_str})'
                )
                logger.info(f"[IMAP] Searching unseen emails SINCE {since_str} (campaign month)")
            else:
                # Fallback: scan last 30 days if no campaigns found
                since_date = datetime.now(timezone.utc) - timedelta(days=30)
                since_str = imap_date(since_date)
                status, msg_ids = await asyncio.to_thread(
                    conn.search, None, f'(UNSEEN SINCE {since_str})'
                )
                logger.warning(f"[IMAP] No campaigns found, searching unseen emails SINCE {since_str}")

            if status != "OK" or not msg_ids[0]:
                return {"scanned": 0, "matched": 0, "processed": 0, "errors": 0,
                        "message": "No unseen emails found in inbox for the campaign period"}

            ids = msg_ids[0].split()
            scanned = len(ids)
            logger.info(f"[IMAP] Found {scanned} unseen emails in campaign period, scanning")

            for msg_id in ids:
                try:
                    status, data = await asyncio.to_thread(conn.fetch, msg_id, "(RFC822)")
                    if status != "OK":
                        continue

                    raw_email = data[0][1]
                    msg = email.message_from_bytes(raw_email, policy=email.policy.default)

                    # Dedup: skip if we already processed this email (e.g. it
                    # was handled before messages started being marked seen)
                    msg_message_id = msg.get("Message-ID", "").strip()
                    if msg_message_id and msg_message_id in processed_msg_ids:
                        seen_skips.append(msg_id)
                        continue

                    body = _extract_body(msg)
                    # Only scan the user's actual reply, NOT quoted
                    # template content (which may contain "Remove me" etc.)
                    reply_text = _extract_reply_only(body)

                    sender_email_preview = _extract_sender_email(msg)
                    subject_preview = msg.get("Subject", "(no subject)")

                    keyword_match = KEYWORD_PATTERN.search(reply_text)

                    if not keyword_match:
                        # Log if the full body HAD keywords but reply didn't
                        if KEYWORD_PATTERN.search(body):
                            logger.warning(f"[IMAP] FILTERED OUT: {sender_email_preview} "
                                  f"('{subject_preview}') — keyword only in "
                                  f"quoted/template content, not in actual reply")
                        seen_skips.append(msg_id)
                        continue

                    matched += 1
                    sender_email = _extract_sender_email(msg)
                    if not sender_email:
                        logger.warning(
                            f"[IMAP] Could not extract sender email from "
                            f"message {msg_id} ('{subject_preview}')"
                        )
                        seen_skips.append(msg_id)
                        continue

                    # Skip if this sender was already processed
                    if sender_email in processed_emails_set:
                        seen_skips.append(msg_id)
                        continue

                    matched_keyword = keyword_match.group(0).lower()
                    subject = msg.get("Subject", "(no subject)")
                    logger.info(f"[IMAP] Keyword match: {sender_email} ('{matched_keyword}')")

                    # Parse email date for campaign matching
                    email_date = None
                    date_str = msg.get("Date", "")
                    if date_str:
                        try:
                            dt = email.utils.parsedate_to_datetime(date_str)
                            if dt.tzinfo is not None:
                                email_date = dt.astimezone(timezone.utc)
                            else:
                                email_date = dt.replace(tzinfo=timezone.utc)
                        except Exception:
                            email_date = datetime.now(timezone.utc)
                    else:
                        email_date = datetime.now(timezone.utc)

                    # Look up subscriber in ListMonk first so we know which
                    # lists they're on before attributing to a campaign.
                    safe_email = safe_email_for_query(sender_email)
                    if not safe_email:
                        logger.warning(f"Invalid email format, skipping: {sender_email}")
                        seen_skips.append(msg_id)
                        continue
                    try:
                        result = await client.get_subscribers(
                            1, 1, f"subscribers.email = '{safe_email}'"
                        )
                        subscribers = result.get("data", {}).get("results", [])

                        if not subscribers:
                            logger.info(f"Sender {sender_email} not found in ListMonk, skipping")
                            seen_skips.append(msg_id)
                            continue

                        subscriber = subscribers[0]
                        sub_id = subscriber["id"]
                        sub_lists = [lst["id"] for lst in subscriber.get("lists", [])]

                        # Only process if subscriber belongs to a campaign's
                        # target list — otherwise skip entirely.
                        campaign = _match_campaign(
                            campaigns_list, email_date, set(sub_lists)
                        )
                        if not campaign:
                            logger.info(f"[IMAP] No list-matched campaign for '{subject}' from {sender_email}; skipping")
                            seen_skips.append(msg_id)
                            continue

                        # Unsubscribe from all lists
                        if sub_lists:
                            await client.modify_list_memberships({
                                "ids": [sub_id],
                                "action": "unsubscribe",
                                "target_list_ids": sub_lists,
                                "status": "unsubscribed",
                            })

                        # Conditionally blocklist based on user setting
                        scan_settings = load_settings()
                        if scan_settings.get("blocklist_enabled", False):
                            await client.blocklist_subscriber(sub_id)
                            logger.info(f"[IMAP] Blocklisted: {sender_email}")

                        record = {
                            "email": sender_email,
                            "name": subscriber.get("name", ""),
                            "source": "email",
                            "keyword": matched_keyword,
                            "subject": subject,
                            "message_id": msg_message_id,
                            "campaign_key": f"{email_date.year}-{email_date.month:02d}",
                            "campaign_id": campaign["campaign_id"],
                            "campaign_name": campaign["campaign_name"],
                            "matched_list_id": campaign.get("matched_list_id"),
                            "subscriber_id": sub_id,
                            "lists_removed": sub_lists,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                        new_records.append(record)
                        seen_after_persist.append(msg_id)
                        processed_emails_set.add(sender_email)  # Prevent duplicates in same scan
                        processed += 1
                        action = "Unsubscribed + Blocklisted" if scan_settings.get("blocklist_enabled") else "Unsubscribed"
                        logger.info(f"{action}: {sender_email} (campaign: {campaign['campaign_name']})")

                    except Exception as e:
                        errors += 1
                        logger.error(f"Failed to process {sender_email}: {e}")

                except Exception as e:
                    errors += 1
                    logger.error(f"Failed to parse email {msg_id}: {e}")

            # Persist new records first, then mark their messages \Seen so a
            # crash cannot silently lose an unsubscribe. Skipped messages are
            # safe to mark immediately.
            if new_records:
                await append_log(new_records)
                await mark_processed(r["email"] for r in new_records)

            # Mark skipped messages seen immediately; successfully processed
            # ones only after their records are persisted (a crash in between
            # leaves them unseen so the unsubscribe can be retried).
            for mid in seen_skips + seen_after_persist:
                try:
                    await asyncio.to_thread(conn.store, mid, "+FLAGS", "\\Seen")
                except Exception as e:
                    logger.warning(f"[IMAP] Could not mark message {mid} as seen: {e}")

        except Exception as e:
            logger.error(f"IMAP scan error: {e}")
            errors += 1
        finally:
            try:
                await asyncio.to_thread(conn.logout)
            except Exception:
                pass

        return {
            "scanned": scanned,
            "matched": matched,
            "processed": processed,
            "errors": errors,
            "message": f"Scan complete: {processed} unsubscribed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def get_stats() -> dict:
    """Return aggregate stats from the unsubscribe log."""
    records = load_log()
    total = len(records)

    now = datetime.now(timezone.utc)
    today_str = now.date().isoformat()
    week_ago = now - timedelta(days=7)

    today_count = 0
    week_count = 0
    for r in records:
        ts = r.get("timestamp", "")
        if ts.startswith(today_str):
            today_count += 1
        try:
            dt = datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            continue
        # Legacy records may store naive timestamps; treat them as UTC.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= week_ago:
            week_count += 1

    # Source breakdown — records without 'source' are treated as 'email'
    link_count = sum(1 for r in records if r.get("source") == "link")
    email_count = total - link_count

    return {
        "total": total,
        "today": today_count,
        "this_week": week_count,
        "link_count": link_count,
        "email_count": email_count,
    }
