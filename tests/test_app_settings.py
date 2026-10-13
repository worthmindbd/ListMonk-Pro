"""Tests for the installation-wide app settings (mode onboarding state).

Pins down the contract the SPA relies on: the mode choice is persisted
server-side so the onboarding modal shows up exactly once per installation,
regardless of browser, device, or cleared site data.
"""

import asyncio
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    import app.services.app_settings as app_settings

    monkeypatch.setattr(app_settings, "APP_SETTINGS_FILE", tmp_path / "app_settings.json")
    return app_settings


def test_default_state_is_unselected(settings_file):
    assert settings_file.load_settings() == {"mode": "newsletter", "mode_selected": False}


def test_get_returns_unselected_before_onboarding(client, settings_file):
    resp = client.get("/api/app-settings")
    assert resp.status_code == 200
    assert resp.json() == {"mode": "newsletter", "mode_selected": False}


def test_put_persists_mode_and_marks_selected(client, settings_file):
    resp = client.put("/api/app-settings", json={"mode": "cold_email"})
    assert resp.status_code == 200
    assert resp.json() == {"mode": "cold_email", "mode_selected": True}
    # Survives a reload: the state must come back from disk, not memory.
    assert settings_file.load_settings() == {"mode": "cold_email", "mode_selected": True}


def test_put_rejects_invalid_mode(client, settings_file):
    resp = client.put("/api/app-settings", json={"mode": "spam"})
    assert resp.status_code == 400
    assert settings_file.load_settings()["mode_selected"] is False


def test_put_rejects_malformed_bodies(client, settings_file):
    assert client.put("/api/app-settings", json=[1]).status_code == 400
    assert client.put("/api/app-settings", json={"nomode": True}).status_code == 400
    assert settings_file.load_settings()["mode_selected"] is False


def test_corrupt_file_falls_back_to_defaults(settings_file):
    settings_file.APP_SETTINGS_FILE.write_text("{not json")
    assert settings_file.load_settings() == {"mode": "newsletter", "mode_selected": False}


def test_hand_edited_mode_falls_back_to_default(settings_file):
    settings_file.APP_SETTINGS_FILE.write_text('{"mode": "bogus", "mode_selected": true}')
    state = settings_file.load_settings()
    assert state["mode"] == "newsletter"
    assert state["mode_selected"] is True


def test_set_mode_rejects_invalid_value(settings_file):
    with pytest.raises(ValueError):
        asyncio.run(settings_file.set_mode("bogus"))
