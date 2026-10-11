"""Shared pytest fixtures.

Several services memoise state in module-level globals (campaign history,
opener sets, hard-bounce counts, the unsubscribe processed set). Those caches
are correct in production but leak between tests when one test populates them
and a later test asserts against a fresh mock. Resetting them per test keeps
ordering from silently changing results.
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_module_caches():
    from app.services import hard_bounce_cache, link_unsubscribe, opener_cache

    def _reset():
        link_unsubscribe.invalidate_campaign_history()
        opener_cache.invalidate()
        hard_bounce_cache._hard_bounce_counts = {}
        hard_bounce_cache._last_updated = ""

    _reset()
    yield
    _reset()
