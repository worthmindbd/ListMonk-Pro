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
def _isolate_state_files(tmp_path_factory, monkeypatch):
    """Point every DATA_DIR state file at a per-test temp dir.

    Most tests patch the files they touch, but a scan or service call that
    reaches a state file the test did not anticipate (a newly introduced one,
    or a side path such as the unsubscribe examined set) used to read and
    write the developer's real files in the repo root. Tests that patch a
    path themselves simply override this default.
    """
    from app.services import (
        app_settings, bounce_ingest, campaign_scheduler, reply_log, unsubscribe_log,
    )

    # A directory of its own, not tmp_path: tests list tmp_path's contents.
    state = tmp_path_factory.mktemp("state")
    monkeypatch.setattr(unsubscribe_log, "LOG_FILE", state / "unsubscribe_log.json")
    monkeypatch.setattr(unsubscribe_log, "SETTINGS_FILE", state / "unsubscribe_settings.json")
    monkeypatch.setattr(unsubscribe_log, "PROCESSED_FILE", state / "unsubscribe_processed.json")
    monkeypatch.setattr(unsubscribe_log, "EXAMINED_FILE", state / "unsubscribe_examined.json")
    monkeypatch.setattr(reply_log, "LOG_FILE", state / "reply_log.json")
    monkeypatch.setattr(reply_log, "SETTINGS_FILE", state / "reply_settings.json")
    monkeypatch.setattr(reply_log, "PROCESSED_FILE", state / "reply_processed.json")
    monkeypatch.setattr(bounce_ingest, "PROCESSED_FILE", state / "bounce_processed.json")
    monkeypatch.setattr(campaign_scheduler, "SCHEDULE_FILE", state / "schedule.json")
    monkeypatch.setattr(app_settings, "APP_SETTINGS_FILE", state / "app_settings.json")
    from app import auth
    monkeypatch.setattr(auth, "REVOCATIONS_FILE", state / "session_revocations.json")


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
        # Logout revocations are cached in memory too (rule 11).
        auth._revoked = {}
        auth._revoked_mtime = None

    _reset()
    yield
    _reset()
