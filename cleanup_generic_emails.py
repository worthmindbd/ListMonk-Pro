#!/usr/bin/env python3
"""
Cleanup script to remove subscribers with generic/role-based email addresses.
These are typically not good fits for campaigns (support@, help@, ticket@, info@, etc.)
"""

import asyncio
import httpx
import os
from dotenv import load_dotenv

load_dotenv()

LISTMONK_URL = os.getenv("LISTMONK_URL")
LISTMONK_USER = os.getenv("LISTMONK_USER")
LISTMONK_API_KEY = os.getenv("LISTMONK_API_KEY")

# Generic/role-based email prefixes to filter out
# NOTE: info@ is intentionally excluded per user request
GENERIC_PREFIXES = [
    "support@",
    "help@",
    "ticket@",
    "admin@",
    "contact@",
    "sales@",
    "billing@",
    "service@",
    "noreply@",
    "no-reply@",
    "mailer-daemon@",
    "postmaster@",
    "webmaster@",
    "hostmaster@",
    "abuse@",
    "security@",
    "marketing@",
    "newsletter@",
    "news@",
    "press@",
    "media@",
    "hr@",
    "jobs@",
    "careers@",
    "recruitment@",
    "office@",
    "team@",
    "staff@",
    "general@",
    "enquiries@",
    "inquiries@",
    "feedback@",
    "complaints@",
    "accounts@",
    "finance@",
    "legal@",
    "compliance@",
    "privacy@",
    "data@",
    "operations@",
    "management@",
    "ceo@",
    "cfo@",
    "cto@",
    "coo@",
    "director@",
    "manager@",
]


async def get_all_subscribers(client):
    """Fetch all subscribers from ListMonk using pagination."""
    all_subscribers = []
    page = 1
    per_page = 100
    
    while True:
        response = await client.get(
            "/api/subscribers",
            params={"page": page, "per_page": per_page}
        )
        response.raise_for_status()
        data = response.json()
        
        results = data.get("data", {}).get("results", [])
        if not results:
            break
            
        all_subscribers.extend(results)
        
        total = data.get("data", {}).get("total", 0)
        if len(all_subscribers) >= total:
            break
            
        page += 1
        
    return all_subscribers


def is_generic_email(email):
    """Check if an email matches any generic/role-based pattern."""
    email_lower = email.lower().strip()
    for prefix in GENERIC_PREFIXES:
        if email_lower.startswith(prefix):
            return True
    return False


async def delete_subscribers(client, subscriber_ids):
    """Delete subscribers in bulk using query parameters."""
    if not subscriber_ids:
        return {"ok": 0, "errors": [], "failed": []}
    
    # ListMonk's bulk delete endpoint uses query params: ?id=1&id=2&id=3
    response = await client.delete(
        "/api/subscribers",
        params={"id": subscriber_ids}
    )
    response.raise_for_status()
    return response.json()


async def main():
    print("=" * 80)
    print("ListMonk Generic Email Cleanup Script")
    print("=" * 80)
    print()
    
    async with httpx.AsyncClient(
        base_url=LISTMONK_URL,
        auth=(LISTMONK_USER, LISTMONK_API_KEY),
        timeout=60.0
    ) as client:
        # Step 1: Fetch all subscribers
        print("📥 Fetching all subscribers...")
        subscribers = await get_all_subscribers(client)
        print(f"   Found {len(subscribers)} total subscribers")
        print()
        
        # Step 2: Filter for generic emails
        print("🔍 Scanning for generic/role-based emails...")
        generic_subscribers = []
        for sub in subscribers:
            email = sub.get("email", "")
            if is_generic_email(email):
                generic_subscribers.append(sub)
        
        print(f"   Found {len(generic_subscribers)} subscribers with generic emails")
        print()
        
        if not generic_subscribers:
            print("✅ No generic emails found! Your list is clean.")
            return
        
        # Step 3: Display what will be deleted
        print("=" * 80)
        print("⚠️  SUBSCRIBERS TO BE DELETED:")
        print("=" * 80)
        for i, sub in enumerate(generic_subscribers, 1):
            email = sub.get("email", "N/A")
            name = sub.get("name", "N/A")
            sub_id = sub.get("id", "N/A")
            lists = sub.get("lists", [])
            list_names = [l.get("name", "?") for l in lists] if lists else ["None"]
            print(f"{i:3d}. {email:<45} | Name: {name:<20} | ID: {sub_id}")
            print(f"       Lists: {', '.join(list_names)}")
        print("=" * 80)
        print()
        
        # Step 4: Confirm deletion
        print(f"📊 Summary:")
        print(f"   Total subscribers: {len(subscribers)}")
        print(f"   Generic emails to delete: {len(generic_subscribers)}")
        print(f"   Remaining after cleanup: {len(subscribers) - len(generic_subscribers)}")
        print()
        
        confirm = input("❓ Are you sure you want to delete these subscribers? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("❌ Deletion cancelled.")
            return
        
        # Step 5: Delete in batches (ListMonk may have limits)
        print()
        print("🗑️  Deleting subscribers...")
        ids_to_delete = [sub["id"] for sub in generic_subscribers]
        
        # Delete in batches of 100 to avoid timeouts
        batch_size = 100
        total_deleted = 0
        total_errors = []
        
        for i in range(0, len(ids_to_delete), batch_size):
            batch = ids_to_delete[i:i + batch_size]
            try:
                result = await delete_subscribers(client, batch)
                deleted_count = len(batch) - len(result.get("failed", []))
                total_deleted += deleted_count
                
                if result.get("errors"):
                    total_errors.extend(result["errors"])
                
                print(f"   Batch {i//batch_size + 1}: Deleted {deleted_count}/{len(batch)}")
            except Exception as e:
                print(f"   ❌ Error deleting batch {i//batch_size + 1}: {e}")
                total_errors.append(str(e))
        
        print()
        print("=" * 80)
        print("✅ CLEANUP COMPLETE!")
        print("=" * 80)
        print(f"   Successfully deleted: {total_deleted}")
        if total_errors:
            print(f"   Errors encountered: {len(total_errors)}")
            for err in total_errors[:5]:  # Show first 5 errors
                print(f"      - {err}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
