import logging

import httpx
from typing import Any, Optional
from app.config import settings

logger = logging.getLogger("listmonk_client")


class ListMonkClient:
    """Async client wrapping all ListMonk API calls with Basic Auth."""

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self):
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            auth=(settings.listmonk_user, settings.listmonk_api_key),
            timeout=120.0,
        )

    async def close(self):
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if not self._client:
            raise RuntimeError("ListMonkClient not started. Call start() first.")
        return self._client

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = await self.client.request(method, path, **kwargs)
        resp.raise_for_status()
        return resp.json()

    async def _request_raw(self, method: str, path: str, **kwargs) -> httpx.Response:
        resp = await self.client.request(method, path, **kwargs)
        resp.raise_for_status()
        return resp

    async def paginate_all(self, fetch_fn, per_page: int = 500, max_pages: int = 0, **kwargs) -> list[dict]:
        """Fetch all pages of a paginated endpoint into a single list.

        Args:
            fetch_fn: An async callable accepting page and per_page kwargs,
                e.g. ``self.get_bounces``. Must return ``{"data": {"results": [...], "total": N}}``.
            per_page: Page size for each request.
            max_pages: Optional hard cap on requests. 0 means unlimited. A cap
                prevents an endpoint that keeps returning rows from looping
                forever; a truncated result is logged rather than silently used.
            **kwargs: Additional keyword arguments forwarded to fetch_fn.
        """
        items = []
        async for item in self.iter_pages(fetch_fn, per_page=per_page, max_pages=max_pages, **kwargs):
            items.append(item)
        return items

    async def iter_pages(self, fetch_fn, per_page: int = 500, max_pages: int = 0, **kwargs):
        """Yield items from a paginated endpoint one page at a time.

        Unlike ``paginate_all`` this never holds more than one page in memory,
        which is what makes large exports streamable. Stops on an empty page,
        once ``total`` is satisfied, or after ``max_pages`` requests.
        """
        page = 1
        while True:
            result = await fetch_fn(page=page, per_page=per_page, **kwargs)
            data = result.get("data", {})
            results = data.get("results", [])
            if not results:
                return
            for item in results:
                yield item

            total = data.get("total")
            if isinstance(total, (int, float)) and page * per_page >= total:
                return
            if max_pages and page >= max_pages:
                logger.warning(
                    "Pagination stopped at %d pages (%d items collected, total=%s)",
                    max_pages, page * per_page, total,
                )
                return
            page += 1

    # ── Subscribers ──────────────────────────────────────────

    async def get_subscribers(self, page: int = 1, per_page: int = 50,
                              query: str = "", list_id: Optional[int] = None) -> dict:
        params = {"page": page, "per_page": per_page}
        if query:
            params["query"] = query
        if list_id:
            params["list_id"] = list_id
        return await self._request("GET", "/api/subscribers", params=params)

    async def get_subscribers_by_list_status(
        self,
        list_id: int,
        subscription_status: str,
        page: int = 1,
        per_page: int = 100,
    ) -> dict:
        """Get subscribers for a list filtered by their subscription status."""
        params = {
            "list_id": list_id,
            "subscription_status": subscription_status,
            "page": page,
            "per_page": per_page,
        }
        return await self._request("GET", "/api/subscribers", params=params)

    async def get_subscriber(self, subscriber_id: int) -> dict:
        return await self._request("GET", f"/api/subscribers/{subscriber_id}")

    async def create_subscriber(self, data: dict) -> dict:
        return await self._request("POST", "/api/subscribers", json=data)

    async def update_subscriber(self, subscriber_id: int, data: dict) -> dict:
        return await self._request("PUT", f"/api/subscribers/{subscriber_id}", json=data)

    async def delete_subscriber(self, subscriber_id: int) -> dict:
        return await self._request("DELETE", f"/api/subscribers/{subscriber_id}")

    async def delete_subscribers(self, ids: list[int]) -> dict:
        return await self._request("DELETE", "/api/subscribers", params={"id": ids})

    async def blocklist_subscriber(self, subscriber_id: int) -> dict:
        return await self._request("PUT", f"/api/subscribers/{subscriber_id}/blocklist")

    async def blocklist_subscribers(self, ids: list[int]) -> dict:
        return await self._request("PUT", "/api/subscribers/blocklist", json={"ids": ids})

    async def modify_list_memberships(self, data: dict) -> dict:
        return await self._request("PUT", "/api/subscribers/lists", json=data)

    async def export_subscriber(self, subscriber_id: int) -> dict:
        return await self._request("GET", f"/api/subscribers/{subscriber_id}/export")

    async def get_subscriber_bounces(self, subscriber_id: int) -> dict:
        return await self._request("GET", f"/api/subscribers/{subscriber_id}/bounces")

    async def send_optin(self, subscriber_id: int) -> dict:
        return await self._request("POST", f"/api/subscribers/{subscriber_id}/optin")

    # ── Lists ────────────────────────────────────────────────

    async def get_lists(self, page: int = 1, per_page: int = 50,
                        query: str = "", status: str = "",
                        order_by: str = "created_at", order: str = "DESC",
                        minimal: bool = False, tag: Optional[list[str]] = None) -> dict:
        params: dict[str, Any] = {
            "page": page, "per_page": per_page,
            "order_by": order_by, "order": order,
        }
        if query:
            params["query"] = query
        if status:
            params["status"] = status
        if minimal:
            params["minimal"] = "true"
        if tag:
            params["tag"] = tag
        return await self._request("GET", "/api/lists", params=params)

    async def get_list(self, list_id: int) -> dict:
        return await self._request("GET", f"/api/lists/{list_id}")

    async def create_list(self, data: dict) -> dict:
        return await self._request("POST", "/api/lists", json=data)

    async def update_list(self, list_id: int, data: dict) -> dict:
        return await self._request("PUT", f"/api/lists/{list_id}", json=data)

    async def delete_list(self, list_id: int) -> dict:
        return await self._request("DELETE", f"/api/lists/{list_id}")

    # ── Campaigns ────────────────────────────────────────────

    async def get_campaigns(self, page: int = 1, per_page: int = 50,
                            query: str = "", status: str = "",
                            order_by: str = "created_at", order: str = "DESC",
                            tag: Optional[list[str]] = None) -> dict:
        params: dict[str, Any] = {
            "page": page, "per_page": per_page,
            "order_by": order_by, "order": order,
        }
        if query:
            params["query"] = query
        if status:
            params["status"] = status
        if tag:
            params["tag"] = tag
        return await self._request("GET", "/api/campaigns", params=params)

    async def get_campaign(self, campaign_id: int) -> dict:
        return await self._request("GET", f"/api/campaigns/{campaign_id}")

    async def create_campaign(self, data: dict) -> dict:
        return await self._request("POST", "/api/campaigns", json=data)

    async def update_campaign(self, campaign_id: int, data: dict) -> dict:
        return await self._request("PUT", f"/api/campaigns/{campaign_id}", json=data)

    async def delete_campaign(self, campaign_id: int) -> dict:
        return await self._request("DELETE", f"/api/campaigns/{campaign_id}")

    async def delete_campaigns(self, ids: list[int]) -> dict:
        return await self._request("DELETE", "/api/campaigns", params={"id": ids})

    async def change_campaign_status(self, campaign_id: int, status: str) -> dict:
        return await self._request("PUT", f"/api/campaigns/{campaign_id}/status",
                                   json={"status": status})

    async def preview_campaign(self, campaign_id: int) -> httpx.Response:
        return await self._request_raw("GET", f"/api/campaigns/{campaign_id}/preview")

    async def test_campaign(self, campaign_id: int, data: dict) -> dict:
        return await self._request("POST", f"/api/campaigns/{campaign_id}/test", json=data)

    async def get_running_stats(self, campaign_id: Optional[int] = None) -> dict:
        params = {}
        if campaign_id:
            params["campaign_id"] = campaign_id
        return await self._request("GET", "/api/campaigns/running/stats", params=params)

    async def get_campaign_analytics(self, analytics_type: str,
                                     campaign_id: int = 0,
                                     from_date: str = "", to_date: str = "") -> dict:
        params: dict[str, Any] = {}
        if campaign_id:
            params["id"] = campaign_id
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        return await self._request("GET", f"/api/campaigns/analytics/{analytics_type}",
                                   params=params)

    async def archive_campaign(self, campaign_id: int) -> dict:
        return await self._request("PUT", f"/api/campaigns/{campaign_id}/archive")

    # ── Templates ────────────────────────────────────────────

    async def get_templates(self) -> dict:
        return await self._request("GET", "/api/templates")

    async def get_template(self, template_id: int) -> dict:
        return await self._request("GET", f"/api/templates/{template_id}")

    async def create_template(self, data: dict) -> dict:
        return await self._request("POST", "/api/templates", json=data)

    async def update_template(self, template_id: int, data: dict) -> dict:
        return await self._request("PUT", f"/api/templates/{template_id}", json=data)

    async def set_default_template(self, template_id: int) -> dict:
        return await self._request("PUT", f"/api/templates/{template_id}/default")

    async def delete_template(self, template_id: int) -> dict:
        return await self._request("DELETE", f"/api/templates/{template_id}")

    # ── Bounces ──────────────────────────────────────────────

    async def get_bounces(self, page: int = 1, per_page: int = 50,
                          campaign_id: Optional[int] = None,
                          source: str = "",
                          bounce_type: str = "") -> dict:
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        if campaign_id:
            params["campaign_id"] = campaign_id
        if source:
            params["source"] = source
        if bounce_type:
            params["bounce_type"] = bounce_type
        return await self._request("GET", "/api/bounces", params=params)

    async def delete_bounce(self, bounce_id: int) -> dict:
        return await self._request("DELETE", f"/api/bounces/{bounce_id}")

    async def create_bounce(self, email: str,
                            campaign_uuid: Optional[str] = None,
                            bounce_type: str = "soft",
                            source: str = "api",
                            meta: dict = None) -> dict:
        """Post a bounce to ListMonk's generic bounce webhook. Requires
        'Enable bounce webhooks' in ListMonk → Settings → Bounces.
        Uses regular admin Basic auth (same API credentials as /api)."""
        payload = {
            "email": email,
            "source": source,
            "type": bounce_type,
            "meta": meta or {},
        }
        if campaign_uuid:
            payload["campaign_uuid"] = campaign_uuid
        resp = await self.client.post("/webhooks/bounce", json=payload)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"ok": True}

    async def delete_all_bounces(self) -> dict:
        return await self._request("DELETE", "/api/bounces", params={"all": True})

    # ── Import ───────────────────────────────────────────────

    async def import_subscribers(self, file_content: bytes, filename: str,
                                 params: dict) -> dict:
        import json
        files = {"file": (filename, file_content, "text/csv")}
        data = {"params": json.dumps(params)}
        return await self._request("POST", "/api/import/subscribers",
                                   files=files, data=data)

    async def get_import_status(self) -> dict:
        return await self._request("GET", "/api/import/subscribers")

    async def get_import_logs(self) -> dict:
        return await self._request("GET", "/api/import/subscribers/logs")

    # ── Settings & Deliverability ───────────────────────────

    async def get_settings(self) -> dict:
        return await self._request("GET", "/api/settings")

    async def update_settings(self, data: dict) -> dict:
        return await self._request("PUT", "/api/settings", json=data)


# Singleton instance
listmonk = ListMonkClient()
