import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from tests.conftest import CSRFTestClient
from app.main import app
from app.services.imap_reply_tracker import classify_sentiment
from app.services.deliverability_service import get_deliverability_checklist, apply_deliverability_preset


def test_classify_sentiment():
    # Out of office
    assert classify_sentiment("Out of office", "I am out of the office until Monday") == "auto_reply"
    assert classify_sentiment("Re: Quick question", "Automatic reply: away from my desk") == "auto_reply"

    # Negative
    assert classify_sentiment("Re: Hello", "Please remove me from your list") == "negative"
    assert classify_sentiment("Re: Offer", "Not interested, unsubscribe") == "negative"

    # Positive
    assert classify_sentiment("Re: Video production", "Sounds good, let's talk tomorrow") == "positive"
    assert classify_sentiment("Re: Quick question", "Can you send over pricing and schedule a demo?") == "positive"
    assert classify_sentiment("Re: Followup", "Yes please, happy to connect") == "positive"

    # A reply matching no keyword is a real human response — neither an
    # auto-response nor a rejection — so it falls back to neutral (not positive).
    # False positives (marking ambiguous replies as hot leads) are worse than
    # leaving genuinely interested prospects unflagged for manual review.
    assert classify_sentiment("Re: Inquiry", "How does this work with our existing stack?") == "neutral"


def test_classify_sentiment_polite_decline_is_negative():
    # A courteous "no" carries no blunt refusal phrase; it must not be
    # misread as a positive reply (regression: a real prospect reply
    # saying exactly this was badged "Question").
    decline = (
        "Hello Kawser! Thank you very much for reaching out and sharing your "
        "proposal with us.\n\n"
        "At this time, we are not exploring this type of collaboration, but we "
        "have saved your contact information and will certainly get in touch if "
        "the situation changes. We truly appreciate your professionalism and "
        "wish you great success with your projects.\n\n"
        "Wishing you a productive and wonderful day ahead!"
    )
    assert classify_sentiment("Re: Question About Rollerads?", decline) == "negative"
    assert classify_sentiment(
        "Re: Offer", "We're not currently looking for a new vendor, but thanks anyway"
    ) == "negative"
    assert classify_sentiment(
        "Re: Offer", "Not a good fit for us right now — best of luck!"
    ) == "negative"
    assert classify_sentiment(
        "Re: Offer", "We have no need for this service at the moment."
    ) == "negative"
    assert classify_sentiment(
        "Re: Offer", "I'll pass on this for now, thanks for thinking of us."
    ) == "negative"
    assert classify_sentiment(
        "Re: Offer", "We'll keep you in mind should our needs change."
    ) == "negative"


# ── Bounce / DSN detection ──────────────────────────────────────────────────
#
# Regression: a mailer-daemon delivery-failure notice matched none of the
# keyword lists, so the unknown-means-positive default tagged it
# "Interested / Lead" and auto-flagged a nonexistent address as a Hot Lead.

_GMAIL_DSN_BODY = (
    "This message was created automatically by mail delivery software.\n\n"
    "A message that you sent could not be delivered to one or more of its\n"
    "recipients. This is a permanent error. The following address(es) failed:\n\n"
    "business@bandalabs.com\n"
    "    host aspmx.l.google.com [74.125.206.27]\n"
    "    SMTP error from remote mail server after RCPT TO:<business@bandalabs.com>:\n"
    "    550-5.1.1 The email account that you tried to reach does not exist. Please try\n"
    "    550-5.1.1 double-checking the recipient's email address for typos or\n"
    "    550-5.1.1 unnecessary spaces. For more information, go to\n"
    "    550-5.1.1 https://support.google.com/mail/?p=NoSuchUser - gsmtp"
)


def test_classify_bounce_subjects_are_never_leads():
    # Exim/cPanel wording — the exact DSN seen poisoning the replies feed.
    assert classify_sentiment(
        "Mail delivery failed: returning message to sender", _GMAIL_DSN_BODY
    ) == "bounce"
    # Gmail native wording.
    assert classify_sentiment(
        "Delivery Status Notification (Failure)",
        "Technical details of permanent failure:\n550 5.1.1 address does not exist",
    ) == "bounce"
    # Outlook / Exchange wording.
    assert classify_sentiment(
        "Undeliverable: Re: Quick question",
        "Your message wasn't delivered because the recipient's email provider rejected it.",
    ) == "bounce"
    # qmail wording.
    assert classify_sentiment(
        "failure notice", "Hi. This is the qmail-send program. I'm afraid I wasn't able to deliver your message."
    ) == "bounce"


def test_classify_bounce_senders_are_never_leads():
    # A mailer-daemon/postmaster sender is a bounce even with generic wording.
    assert classify_sentiment(
        "Undelivered Mail", "See attached transcript.", "mailer-daemon@host.getaevoramedia.com"
    ) == "bounce"
    assert classify_sentiment(
        "Returned mail", "Transcript attached.", "MAILER-DAEMON@mx.example.com"
    ) == "bounce"
    assert classify_sentiment(
        "Re: Offer", "Some automated text.", "postmaster@example.com"
    ) == "bounce"


def test_classify_bounce_beats_quoted_rejection_wording():
    # A DSN body can quote rejection-like lines from the original thread;
    # the delivery failure is still the message's actual sentiment.
    assert classify_sentiment(
        "Mail delivery failed: returning message to sender",
        _GMAIL_DSN_BODY + "\n> Not interested, please remove me.",
    ) == "bounce"


def test_classify_automated_no_reply_senders_are_not_positive():
    # no-reply/notification senders must not fall through to the positive
    # default (their bodies often contain "unsubscribe" footer words, so the
    # automated-sender check runs before the keyword passes).
    assert classify_sentiment(
        "You have a new message", "Click here to manage your notification preferences.",
        "no-reply@linkedin.com",
    ) == "auto_reply"
    assert classify_sentiment(
        "Receipt", "Thanks for your purchase.", "noreply@shop.example"
    ) == "auto_reply"
    # A human sender with the same unknown body falls back to neutral (not
    # positive), since the content lacks clear interest signals.
    assert classify_sentiment(
        "You have a new message", "Click here to manage your notification preferences.",
        "kate@rollerads.com",
    ) == "neutral"


def test_classify_human_replies_unchanged_by_bounce_rules():
    # The new checks must not disturb real prospect classifications.
    assert classify_sentiment("Re: Hello", "Please remove me from your list") == "negative"
    assert classify_sentiment("Re: Followup", "Yes please, happy to connect") == "positive"
    assert classify_sentiment("Out of office", "I am out of the office until Monday") == "auto_reply"


def test_classify_ticket_system_auto_replies():
    """Zendesk / Freshdesk ticket confirmations and generic auto-replies are
    automated responses, not genuine prospect engagement — they must not be
    classified as neutral or negative (which would give them campaign
    attribution and inflate the reply rate)."""
    # Zendesk ticket confirmation
    assert classify_sentiment(
        "Request received: Question About FOREVER ? - Ticket number 514499",
        "Thank you for contacting us! Your request (514499) has been received "
        "and is being reviewed by the FOREVER® Client Care Team. A team member "
        "will be in touch within 24-48 hours.",
        "support@forever.com",
    ) == "auto_reply"

    # Freshdesk ticket confirmation
    assert classify_sentiment(
        "RE: Question About Wildwood Management Group? - [#WNT122339]",
        "Your message has been received, and will be processed soon.",
        "info@wildwood-tx.com",
    ) == "auto_reply"

    # Generic ticket created notification
    assert classify_sentiment(
        "RAR Membership Ticket created: Question About Richmond #MEM00238485",
        "***Membership Ticket created #MEM00238485 ***",
        "membership@rarealtors.com",
    ) == "auto_reply"

    # Generic "we received your message" auto-reply
    assert classify_sentiment(
        "Re: Framediscreet",
        "Hello!\n\nWe have received your message and are working quickly to respond.",
        "info@framediscreet.com",
    ) == "auto_reply"


def test_classify_non_english_ooo():
    """Common non-English out-of-office phrases must be caught so they don't
    fall through to neutral and receive campaign attribution."""
    assert classify_sentiment(
        "Fuera de oficina",
        "Hola\nGracias por tu mensaje.\nestaré de viaje con conectividad reducida.",
        "eric@example.com",
    ) == "auto_reply"
    assert classify_sentiment(
        "Abwesenheitsnotiz",
        "Ich bin bis Montag außer Haus.",
        "hans@example.de",
    ) == "auto_reply"


def test_classify_genuine_replies_not_caught_by_auto_patterns():
    """The expanded auto-reply patterns must not swallow real human replies."""
    assert classify_sentiment("Re: Hi", "hi", "md.kawser.drive2@gmail.com") == "neutral"
    assert classify_sentiment("Re: Proposal", "Yes please, happy to connect") == "positive"
    assert classify_sentiment("Re: Offer", "Not interested, unsubscribe") == "negative"
    assert classify_sentiment("Re: Hello", "Who's this?") == "neutral"
    # A reply that mentions "ticket" in a human context is still a human reply
    assert classify_sentiment(
        "Re: Support", "I opened a ticket last week but haven't heard back",
        "prospect@example.com",
    ) == "neutral"


def test_classify_institutional_auto_acknowledgement():
    """A shared mailbox's auto-acknowledgement is not a prospect reply.

    Regression: the real reply from info@huset-kbh.dk was badged
    "Interested / Lead" and auto-flagged as a Hot Lead. It is an automated
    "thanks, we will get back to you" notice whose promotional footer
    ("if you are interested in renting a venue…") matched the "interested"
    keyword — that footer is the sender's own pitch, not interest in our
    offer.
    """
    huset_ack = (
        "Tak for din mail.\n"
        "Vi vender tilbage hurtigst muligt.\n\n"
        "Hvis du er interesseret i at leje et lokale til møde, firmaevent, "
        "fødselsdag mm. henviser vi til bookbyen.dk.\n\n"
        "Med venlig hilsen,\nHUSET\n"
        "---------------------------\n"
        "Thank you for your attention.\n"
        "We will get back to you as soon as possible.\n\n"
        "If you are interested in renting a venue for a meeting, team event, "
        "birthday party, etc., please visit bookbyen.dk."
    )
    assert classify_sentiment(
        "Tak for din mail! // Thanks for getting in contact with us!",
        huset_ack,
        "info@huset-kbh.dk",
    ) == "auto_reply"


def test_classify_auto_acknowledgement_spellings():
    # "we will" spelled out is the same acknowledgement as "we'll" — only the
    # contraction used to be recognised, so the spelled-out form fell through
    # to the positive pass.
    assert classify_sentiment(
        "Re: Question About Something?",
        "Thank you for your message. We will get back to you as soon as possible.",
        "info@example.com",
    ) == "auto_reply"
    # Nordic shared mailboxes acknowledge in the local language.
    assert classify_sentiment(
        "Tak for din mail!",
        "Tak for din mail.\nVi vender tilbage hurtigst muligt.",
        "info@example.dk",
    ) == "auto_reply"
    assert classify_sentiment(
        "Tack för din mail!",
        "Vi återkommer så snart som möjligt.",
        "info@example.se",
    ) == "auto_reply"
    assert classify_sentiment(
        "Takk for din mail!",
        "Vi kommer tilbake så snart som mulig.",
        "info@example.no",
    ) == "auto_reply"


def test_classify_marketing_footer_is_not_interest():
    """A keyword inside the sender's own marketing offer is not interest."""
    # The offer owns its sentence, so a second keyword in it is part of the pitch.
    assert classify_sentiment(
        "Re: Question About Venue Hire?",
        "Hi, we rent venues. If you are interested in renting a venue for a "
        "meeting, please visit bookbyen.dk.",
        "info@example.dk",
    ) == "neutral"
    assert classify_sentiment(
        "Re: Offer", "If you are interested in our pricing, let me know.",
        "prospect@example.com",
    ) == "neutral"
    # A first-person statement of interest still counts, even in the same
    # sentence as a third-person offer.
    assert classify_sentiment(
        "Re: Proposal",
        "We are interested — if you are interested in a white-label "
        "partnership too, let's talk.",
        "prospect@example.com",
    ) == "positive"
    assert classify_sentiment(
        "Re: Proposal", "I'm interested. What are your pricing tiers?",
        "prospect@example.com",
    ) == "positive"


@pytest.mark.asyncio
async def test_reclassify_fixes_stored_auto_acknowledgement(tmp_path, monkeypatch):
    """The stored auto-flagged acknowledgement is corrected on the next scan.

    The record was already processed, so no later scan re-reads it — only the
    re-classification pass can retire its "Interested / Lead" badge. The
    classifier's own lead promotion is undone with it; a status a human set
    is left alone.
    """
    import app.services.reply_log as rl
    from app.services.imap_reply_tracker import reclassify_stored_sentiments

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "reply_log.json")
    monkeypatch.setattr(rl, "PROCESSED_FILE", tmp_path / "reply_processed.json")
    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    rl.save_log([{
        "id": "rep_huset", "message_id": "<huset@huset-kbh.dk>",
        "from_email": "info@huset-kbh.dk",
        "subject": "Tak for din mail! // Thanks for getting in contact with us!",
        "full_body": (
            "Tak for din mail.\nVi vender tilbage hurtigst muligt.\n\n"
            "If you are interested in renting a venue for a meeting, team "
            "event, birthday party, etc., please visit bookbyen.dk."
        ),
        "sentiment": "positive",   # what the old classifier stored
        "status": "lead",          # auto-promoted at ingestion
        "status_source": "auto",
        "campaign_id": 6, "campaign_name": "Mixed Leads 12K",
    }])

    changed = await reclassify_stored_sentiments()
    assert changed == 1
    rec = rl.load_log()[0]
    assert rec["sentiment"] == "auto_reply"
    assert rec["status"] == "unread"          # the auto-promotion is undone
    assert rec["campaign_id"] is None         # and the campaign attribution
    assert rec["campaign_name"] is None       # goes with it


@pytest.mark.asyncio
async def test_reclassify_purges_stored_bounce_records(tmp_path, monkeypatch):
    """Records the old classifier stored from DSNs (unknown-means-positive
    default even hot-flagged them) are removed from the feed by the next
    scan: delivery failures are not replies. Genuine positives stay."""
    import app.services.reply_log as rl
    from app.services.imap_reply_tracker import reclassify_stored_sentiments

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "reply_log.json")
    monkeypatch.setattr(rl, "PROCESSED_FILE", tmp_path / "reply_processed.json")
    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    rl.save_log([
        {
            "id": "rep_1",
            "message_id": "<dsn-1@host>",
            "from_email": "mailer-daemon@host.getaevoramedia.com",
            "subject": "Mail delivery failed: returning message to sender",
            "sentiment": "positive",   # what the old classifier stored
            "status": "lead",          # auto-promoted at ingestion
            "full_body": _GMAIL_DSN_BODY,
        },
        {
            "id": "rep_2",
            "message_id": "<yes-1@host>",
            "from_email": "admin@makenibent.com",
            "subject": "Re: Question About Makeni Bent Digital Consulting LLC?",
            "sentiment": "positive",
            "status": "lead",
            "full_body": "Yes. Please share.",
        },
    ])

    changed = await reclassify_stored_sentiments()

    assert changed == 1
    records = rl.load_log()
    assert [r["id"] for r in records] == ["rep_2"]  # the DSN record is gone
    lead = records[0]
    assert lead["sentiment"] == "positive"
    assert lead["status"] == "lead"


@pytest.mark.asyncio
async def test_body_repair_drops_recovered_bounce(tmp_path, monkeypatch):
    """A stored empty reply whose recovered body is a DSN is removed, not
    re-badged: delivery failures belong to the bounce pipeline."""
    import app.services.reply_log as rl
    from app.services.imap_reply_tracker import apply_body_repairs

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "reply_log.json")
    monkeypatch.setattr(rl, "PROCESSED_FILE", tmp_path / "reply_processed.json")
    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    rl.save_log([
        {"id": "rep_1", "message_id": "<dsn@x>", "full_body": "",
         "sentiment": "question", "status": "unread"},
        {"id": "rep_2", "message_id": "<real@x>", "full_body": "text",
         "sentiment": "question", "status": "archived"},
    ])

    changed = await apply_body_repairs({
        "<dsn@x>": {
            "full_body": _GMAIL_DSN_BODY,
            "body_preview": _GMAIL_DSN_BODY[:200],
            "sentiment": "bounce",
        },
    })

    assert changed == 1
    records = rl.load_log()
    assert [r["id"] for r in records] == ["rep_2"]


@pytest.mark.asyncio
async def test_deliverability_checklist_evaluation():
    mock_listmonk = MagicMock()
    mock_listmonk.get_settings = AsyncMock(return_value={
        "data": {
            "privacy.disable_tracking": True,
            "privacy.individual_tracking": False,
            "privacy.unsubscribe_header": True,
            "app.batch_size": 100,
            "app.concurrency": 1,
            "app.enable_public_archive": False,
            # A fully wired instance also needs the bounce loop + safe uploads,
            # otherwise the score is not 100 (the audit used to ignore both).
            "bounce.enabled": True,
            "bounce.mailboxes": [{"enabled": True, "host": "mail.example.com"}],
            "bounce.actions": {"hard": {"count": 1, "action": "blocklist"}},
            "upload.extensions": ["jpg", "png"],
            "app.message_rate": 1,
            "app.message_sliding_window": True,
            "app.message_sliding_window_duration": "2m",
            "app.message_sliding_window_rate": 1,
        }
    })

    result = await get_deliverability_checklist(mock_listmonk)
    assert result["score"] == 100
    assert result["overall_status"] == "optimal"
    assert len(result["checks"]) == 10
    # Informational context (send cadence) is reported but never scored.
    assert [c["id"] for c in result["checks"] if c["status"] == "info"] == ["send_cadence"]


@pytest.mark.asyncio
async def test_deliverability_flags_missing_bounce_loop_and_upload_wildcard():
    mock_listmonk = MagicMock()
    mock_listmonk.get_settings = AsyncMock(return_value={
        "data": {
            "privacy.disable_tracking": True,
            "privacy.individual_tracking": False,
            "privacy.unsubscribe_header": True,
            "app.batch_size": 100,
            "app.concurrency": 1,
            "app.enable_public_archive": False,
            # Placeholder mailbox, no provider, wildcard uploads.
            "bounce.enabled": True,
            "bounce.mailboxes": [{"enabled": False, "host": "pop.yoursite.com"}],
            "bounce.actions": {"hard": {"count": 1, "action": "blocklist"}},
            "upload.extensions": ["jpg", "*"],
        }
    })

    result = await get_deliverability_checklist(mock_listmonk)
    by_id = {c["id"]: c for c in result["checks"]}
    assert by_id["bounce_feedback"]["status"] == "fail"
    assert by_id["upload_extensions"]["status"] == "fail"
    assert by_id["hard_bounce_action"]["status"] == "pass"
    # 9 scored checks (send_cadence is informational), 7 passing.
    assert result["score"] == 77
    assert result["overall_status"] == "good"


@pytest.mark.asyncio
async def test_deliverability_reports_send_cadence_per_day():
    mock_listmonk = MagicMock()
    mock_listmonk.get_settings = AsyncMock(return_value={
        "data": {
            "privacy.disable_tracking": True,
            "privacy.individual_tracking": False,
            "privacy.unsubscribe_header": True,
            "app.batch_size": 100,
            "app.concurrency": 1,
            "app.enable_public_archive": False,
            "bounce.enabled": True,
            "bounce.mailboxes": [{"enabled": True}],
            "bounce.actions": {"hard": {"action": "blocklist"}},
            "upload.extensions": ["png"],
            "app.message_sliding_window": True,
            "app.message_sliding_window_duration": "2m",
            "app.message_sliding_window_rate": 1,
        }
    })

    result = await get_deliverability_checklist(mock_listmonk)
    # 1 message per 2 minutes = 720/day, surfaced so slow drains are visible.
    assert result["raw_settings"]["send_cadence_per_day"] == 720
    cadence = next(c for c in result["checks"] if c["id"] == "send_cadence")
    assert "720" in cadence["current_value"]


@pytest.mark.asyncio
async def test_apply_deliverability_preset():
    mock_listmonk = MagicMock()
    mock_listmonk.apply_settings = AsyncMock(return_value={"data": True})

    res = await apply_deliverability_preset(mock_listmonk, "cold_email")
    assert res["status"] == "ok"
    assert res["applied"]["privacy.disable_tracking"] is True
    # One-click List-Unsubscribe is a Gmail/Yahoo bulk-sender requirement,
    # so the cold preset keeps it on as well.
    assert res["applied"]["privacy.unsubscribe_header"] is True
    assert res["applied"]["app.batch_size"] == 100
    # The preset must merge, not send a partial payload: ListMonk rejects a
    # settings PUT without an SMTP block.
    assert mock_listmonk.apply_settings.await_count == 1
    assert mock_listmonk.update_settings.call_count == 0

    res_nl = await apply_deliverability_preset(mock_listmonk, "newsletter")
    assert res_nl["status"] == "ok"
    assert res_nl["applied"]["privacy.disable_tracking"] is False
    assert res_nl["applied"]["privacy.unsubscribe_header"] is True


# ── Settings round-trip: masked secrets must never be written back ──────────

def test_sanitize_settings_blanks_masked_secrets():
    from app.services.listmonk_client import sanitize_settings_for_update

    raw = {
        "smtp": [{
            "uuid": "4a7163a7-4ded-4349-92d4-973d2820de7b",
            "host": "mail.example.com",
            "password": "\u2022" * 16,
            "from_addresses": ["@example.com"],
        }],
        "bounce.mailboxes": [{"uuid": "b1", "password": "\u2022" * 8, "host": "pop.example.com"}],
        "bounce.postmark": {"username": "postmark", "password": "\u2022" * 5},
        "upload.s3.aws_secret_access_key": "\u2022" * 20,
        "security.oidc": {"client_secret": "\u2022" * 10, "provider_name": "Google"},
        "app.batch_size": 100,
        "privacy.disable_tracking": True,
    }

    clean = sanitize_settings_for_update(raw)

    assert clean["smtp"][0]["password"] == ""
    assert clean["smtp"][0]["uuid"] == raw["smtp"][0]["uuid"]      # UUID kept
    assert clean["smtp"][0]["host"] == "mail.example.com"
    assert clean["smtp"][0]["from_addresses"] == ["@example.com"]
    assert clean["bounce.mailboxes"][0]["password"] == ""
    assert clean["bounce.postmark"]["password"] == ""
    assert clean["upload.s3.aws_secret_access_key"] == ""
    assert clean["security.oidc"]["client_secret"] == ""
    assert clean["security.oidc"]["provider_name"] == "Google"
    assert clean["app.batch_size"] == 100
    assert clean["privacy.disable_tracking"] is True


@pytest.mark.asyncio
async def test_apply_settings_merges_patch(monkeypatch):
    from app.services.listmonk_client import ListMonkClient

    client = ListMonkClient()
    current = {
        "smtp": [{"uuid": "u1", "host": "mail.example.com", "password": "\u2022" * 12}],
        "app.batch_size": 1000,
        "privacy.disable_tracking": False,
    }
    sent: dict = {}

    async def fake_get_settings():
        return {"data": current}

    async def fake_request(method, path, **kwargs):
        sent["method"] = method
        sent["path"] = path
        sent["json"] = kwargs.get("json")
        return {"data": True}

    monkeypatch.setattr(client, "get_settings", fake_get_settings)
    monkeypatch.setattr(client, "_request", fake_request)

    await client.apply_settings({"app.batch_size": 100})

    assert sent["method"] == "PUT" and sent["path"] == "/api/settings"
    assert sent["json"]["app.batch_size"] == 100           # patch applied
    assert sent["json"]["privacy.disable_tracking"] is False  # untouched key preserved
    assert sent["json"]["smtp"][0]["password"] == ""       # secret not clobbered
    assert sent["json"]["smtp"][0]["host"] == "mail.example.com"


@pytest.fixture
def client():
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app)


def test_replies_api_endpoints(client):
    # Test status
    res = client.get("/api/replies/status")
    assert res.status_code == 200
    assert "configured" in res.json()

    # Test stats
    res = client.get("/api/replies/stats")
    assert res.status_code == 200
    assert "total_replies" in res.json()
    assert "reply_rate" in res.json()

    # Test get replies
    res = client.get("/api/replies")
    assert res.status_code == 200
    assert "results" in res.json()


def test_deliverability_api_endpoints(client):
    with patch("app.routers.deliverability.listmonk.get_settings", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = {
            "data": {
                "privacy.disable_tracking": True,
                "privacy.individual_tracking": False,
                "privacy.unsubscribe_header": True,
                "app.batch_size": 100,
                "app.concurrency": 1,
                "app.enable_public_archive": False,
                "bounce.enabled": True,
                "bounce.mailboxes": [{"enabled": True}],
                "bounce.actions": {"hard": {"action": "blocklist"}},
                "upload.extensions": ["png"],
            }
        }
        res = client.get("/api/deliverability/checklist")
        assert res.status_code == 200
        # A fully wired instance scores 100; the audit no longer ignores the
        # bounce mailbox or the upload allow-list.
        assert res.json()["score"] == 100
