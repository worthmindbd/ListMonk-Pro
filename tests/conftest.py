"""Shared pytest fixtures.

Several services memoise state in module-level globals (campaign history,
opener sets, hard-bounce counts, the unsubscribe processed set). Those caches
are correct in production but leak between tests when one test populates them
and a later test asserts against a fresh mock. Resetting them per test keeps
ordering from silently changing results.
"""

import pytest
from starlette.testclient import TestClient


class CSRFTestClient(TestClient):
    """TestClient that sends X-Requested-With on every request so the CSRF
    middleware does not reject state-changing API calls from the test suite."""

    def request(self, *args, **kwargs):
        headers = kwargs.get("headers") or {}
        if isinstance(headers, dict):
            headers.setdefault("X-Requested-With", "XMLHttpRequest")
        kwargs["headers"] = headers
        return super().request(*args, **kwargs)


@pytest.fixture(autouse=True)
def _reset_module_caches():
    from app.services import hard_bounce_cache, link_unsubscribe, opener_cache
    from app.routers import campaigns as campaigns_router, replies as replies_router
    from app import auth

    def _reset():
        link_unsubscribe.invalidate_campaign_history()
        opener_cache.invalidate()
        hard_bounce_cache._hard_bounce_counts = {}
        hard_bounce_cache._last_updated = ""
        hard_bounce_cache._pending_update = None
        # Rule 11 covers router-level caches too: the 60s summary cache would
        # otherwise leak aggregate totals between tests.
        campaigns_router.invalidate_summary_cache()
        replies_router.invalidate_reply_stats_cache()
        # Login rate-limit buckets are per-IP module state; individual tests
        # used to clear them by hand, which is fragile.
        auth._login_failures.clear()

    _reset()
    yield
    _reset()
