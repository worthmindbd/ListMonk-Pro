"""Tests for bounce opener exclusion logic."""

import pytest


def _bounce(email: str, campaign_id: int, bounce_type: str = "hard") -> dict:
    return {
        "id": 1,
        "email": email,
        "type": bounce_type,
        "campaign": {"id": campaign_id, "name": "Test"},
    }


def test_exclude_openers_from_bounces_drops_matching_email():
    from app.services.bounce_filters import exclude_openers_from_bounces

    bounces = [_bounce("alice@example.com", 10), _bounce("bob@example.com", 10)]
    openers = {10: {"alice@example.com"}}

    result = exclude_openers_from_bounces(bounces, openers)
    assert len(result) == 1
    assert result[0]["email"] == "bob@example.com"


def test_exclude_openers_is_case_insensitive():
    from app.services.bounce_filters import exclude_openers_from_bounces

    bounces = [_bounce("Alice@Example.COM", 5)]
    openers = {5: {"alice@example.com"}}

    assert exclude_openers_from_bounces(bounces, openers) == []


def test_exclude_openers_only_for_attributed_campaign():
    from app.services.bounce_filters import exclude_openers_from_bounces

    bounces = [_bounce("alice@example.com", 10), _bounce("alice@example.com", 20)]
    openers = {10: {"alice@example.com"}}

    result = exclude_openers_from_bounces(bounces, openers)
    assert len(result) == 1
    assert result[0]["campaign"]["id"] == 20


def test_exclude_openers_keeps_bounces_without_campaign():
    from app.services.bounce_filters import exclude_openers_from_bounces

    bounces = [{"id": 1, "email": "alice@example.com", "type": "hard"}]
    openers = {10: {"alice@example.com"}}

    assert len(exclude_openers_from_bounces(bounces, openers)) == 1


def test_filter_campaign_hard_bounces_excludes_soft_and_openers():
    from app.services.bounce_filters import exclude_openers_from_bounces

    bounces = [
        _bounce("alice@example.com", 3, "hard"),
        _bounce("bob@example.com", 3, "soft"),
        _bounce("carol@example.com", 3, "hard"),
    ]
    hard = [b for b in bounces if b.get("type") == "hard"]
    openers = {3: {"alice@example.com"}}

    result = exclude_openers_from_bounces(hard, openers)
    assert [b["email"] for b in result] == ["carol@example.com"]
