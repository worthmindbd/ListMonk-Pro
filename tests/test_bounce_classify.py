"""Tests for bounce classification logic.

Classification contract: only truly invalid addresses become HARD. Everything
else (mailbox full, blacklist, reputation, rate limit, content, 4xx) is SOFT
so ListMonk's soft-bounce aggregation — not our ingestion — decides eventual
blocklisting.
"""

import pytest


# ── HARD bounces: permanently invalid recipient ──────────────────────────────

@pytest.mark.parametrize("body", [
    "550 5.1.1 <user@example.com> User unknown",
    "550 5.1.1 The email account that you tried to reach does not exist",
    "smtp; 550-5.1.1 The email account that you tried to reach does not exist",
    "Diagnostic-Code: smtp; 550 5.1.1 no such user",
])
def test_classify_5_1_1_user_unknown_is_hard(body):
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(body=body, subject="Mail delivery failed")
    assert result["type"] == "hard"
    assert "user_unknown" in result["reason"] or "5.1.1" in result["reason"]


def test_classify_5_1_2_host_unknown_is_hard():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="550 5.1.2 Host or domain name not found",
        subject="Undelivered Mail",
    )
    assert result["type"] == "hard"


def test_classify_5_1_3_bad_syntax_is_hard():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="550 5.1.3 Bad destination mailbox address syntax",
        subject="Undelivered",
    )
    assert result["type"] == "hard"


def test_classify_5_1_10_null_mx_is_hard():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="550 5.1.10 Recipient address rejected: null MX",
        subject="Delivery failed",
    )
    assert result["type"] == "hard"


def test_classify_5_2_1_mailbox_disabled_is_hard():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="550 5.2.1 The email account is disabled",
        subject="Delivery failed",
    )
    assert result["type"] == "hard"


def test_classify_5_4_4_no_route_is_hard():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="550 5.4.4 Unable to route: no MX record for domain",
        subject="Undeliverable",
    )
    assert result["type"] == "hard"


@pytest.mark.parametrize("body", [
    "User unknown in virtual alias table",
    "No such user here",
    "recipient address rejected: user unknown",
    "address does not exist",
    "No mailbox here by that name",
    "unknown recipient",
])
def test_classify_user_unknown_keywords_is_hard(body):
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(body=body, subject="")
    assert result["type"] == "hard"


# ── SOFT bounces: mailbox full (best practice — NOT hard) ────────────────────

def test_classify_5_2_2_mailbox_full_is_soft():
    """Mailbox-full is soft per SendGrid/Mailgun/Postmark/SES best practice."""
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="552 5.2.2 Mailbox is full",
        subject="Undeliverable",
    )
    assert result["type"] == "soft"


def test_classify_mailbox_full_keyword_is_soft():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="The recipient's mailbox is full and can't accept new messages",
        subject="",
    )
    assert result["type"] == "soft"


def test_classify_quota_exceeded_is_soft():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="552 Quota exceeded",
        subject="",
    )
    assert result["type"] == "soft"


# ── SOFT bounces: blacklist / reputation / policy ────────────────────────────

def test_classify_spamhaus_is_soft():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="554 5.7.1 Rejected: listed at zen.spamhaus.org",
        subject="",
    )
    assert result["type"] == "soft"


def test_classify_5_7_1_policy_is_soft():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="550 5.7.1 Message rejected due to sender reputation",
        subject="",
    )
    assert result["type"] == "soft"


def test_classify_ip_reputation_is_soft():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="IP address is blocked due to poor reputation",
        subject="",
    )
    assert result["type"] == "soft"


# ── SOFT bounces: 4xx temporary failures ─────────────────────────────────────

def test_classify_421_too_busy_is_soft():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="421 4.7.0 Server busy, try again later",
        subject="Delayed delivery",
    )
    assert result["type"] == "soft"


def test_classify_4_7_1_greylisted_is_soft():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="450 4.7.1 Greylisted, please try again",
        subject="",
    )
    assert result["type"] == "soft"


def test_classify_451_try_again_is_soft():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="451 Temporary local problem - please try later",
        subject="",
    )
    assert result["type"] == "soft"


# ── SOFT bounces: content / size ─────────────────────────────────────────────

def test_classify_5_2_3_message_too_large_is_soft():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="552 5.2.3 Message exceeds size limit",
        subject="",
    )
    assert result["type"] == "soft"


def test_classify_5_6_1_content_rejected_is_soft():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="550 5.6.1 Message rejected: content not acceptable",
        subject="",
    )
    assert result["type"] == "soft"


# ── Ambiguous: default must be SOFT (conservative) ───────────────────────────

def test_classify_unknown_bounce_is_soft():
    """When we can't confidently classify, default to soft — never accidentally
    mark a subscriber as permanently dead."""
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="Mail delivery failed for unknown reasons",
        subject="",
    )
    assert result["type"] == "soft"


def test_classify_empty_body_is_soft():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(body="", subject="")
    assert result["type"] == "soft"


# ── Hard-vs-soft priority: blacklist keywords beat 5.1.x codes ───────────────

def test_blacklist_in_5_1_1_body_still_hard():
    """If both a HARD code and a blacklist keyword are present, HARD wins
    because the 5.1.1 is an authoritative per-address code, while blacklist
    wording could appear in the diagnostic text of any bounce."""
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="550 5.1.1 User unknown (not a blacklist issue)",
        subject="",
    )
    assert result["type"] == "hard"


# ── Reason field carries the matched signal ──────────────────────────────────

def test_reason_field_is_populated():
    from app.services.bounce_ingest import classify_bounce
    result = classify_bounce(
        body="550 5.1.1 User unknown",
        subject="",
    )
    assert "reason" in result
    assert result["reason"]  # non-empty
