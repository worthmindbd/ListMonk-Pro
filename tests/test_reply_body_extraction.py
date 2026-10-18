"""Regression tests: HTML-only replies must never render as "(Empty reply)".

Outlook/mobile clients send replies whose only real content lives in a
text/html part (or with an empty text/plain part beside it). The extractor
used to return "" for those, so the reply feed showed an empty body and
sentiment fell through to the default "question".
"""

import email
import email.policy


def _parse(raw: bytes):
    return email.message_from_bytes(raw, policy=email.policy.default)


HTML_ONLY_MULTIPART = (
    b"From: Prospect <prospect@example.com>\r\n"
    b"To: us@example.com\r\n"
    b"Subject: Re: Question About Makeni Bent Digital Consulting LLC?\r\n"
    b"Message-ID: <html-only@example.com>\r\n"
    b'MIME-Version: 1.0\r\n'
    b'Content-Type: multipart/alternative; boundary="b1"\r\n'
    b"\r\n"
    b"--b1\r\n"
    b'Content-Type: text/html; charset="utf-8"\r\n'
    b"\r\n"
    b"<html><body><p>Yes, we are interested &mdash; send pricing!</p>"
    b"<p>Let&#39;s schedule a call.</p></body></html>\r\n"
    b"--b1--\r\n"
)

EMPTY_PLAIN_PLUS_HTML = (
    b"From: Prospect <prospect@example.com>\r\n"
    b"Subject: Re: Hello\r\n"
    b"Message-ID: <empty-plain@example.com>\r\n"
    b'MIME-Version: 1.0\r\n'
    b'Content-Type: multipart/alternative; boundary="b2"\r\n'
    b"\r\n"
    b"--b2\r\n"
    b'Content-Type: text/plain; charset="utf-8"\r\n'
    b"\r\n"
    b"\r\n"
    b"--b2\r\n"
    b'Content-Type: text/html; charset="utf-8"\r\n'
    b"\r\n"
    b"<html><body><div>Sounds good, tell me more.</div></body></html>\r\n"
    b"--b2--\r\n"
)

PLAIN_AND_HTML = (
    b"From: Prospect <prospect@example.com>\r\n"
    b"Subject: Re: Hello\r\n"
    b"Message-ID: <both@example.com>\r\n"
    b'MIME-Version: 1.0\r\n'
    b'Content-Type: multipart/alternative; boundary="b3"\r\n'
    b"\r\n"
    b"--b3\r\n"
    b'Content-Type: text/plain; charset="utf-8"\r\n'
    b"\r\n"
    b"Plain answer wins.\r\n"
    b"--b3\r\n"
    b'Content-Type: text/html; charset="utf-8"\r\n'
    b"\r\n"
    b"<html><body><p>HTML answer loses.</p></body></html>\r\n"
    b"--b3--\r\n"
)

SINGLE_PART_HTML = (
    b"From: Prospect <prospect@example.com>\r\n"
    b"Subject: Re: Hello\r\n"
    b"Message-ID: <single-html@example.com>\r\n"
    b'MIME-Version: 1.0\r\n'
    b'Content-Type: text/html; charset="utf-8"\r\n'
    b"\r\n"
    b"<html><head><style>p{color:red}</style></head>"
    b"<body><p>Happy to connect next week.</p></body></html>\r\n"
)

# Production shape (Apple Mail): multipart/alternative carrying a single
# quoted-printable text/html part whose <head> holds an UNCLOSED <meta> void
# tag. A skip-depth counter never recovered from that and suppressed the
# whole body; only a tag stack that ignores void elements survives it.
APPLE_MAIL_HTML_ONLY = (
    b"From: Abdul Fattah Ismail <admin@makenibent.com>\r\n"
    b"To: us@example.com\r\n"
    b"Subject: Re: Question About Makeni Bent Digital Consulting LLC?\r\n"
    b"Message-ID: <apple-mail@example.com>\r\n"
    b'MIME-Version: 1.0\r\n'
    b'Content-Type: multipart/alternative; boundary="am1"\r\n'
    b"\r\n"
    b"--am1\r\n"
    b'Content-Type: text/html; charset="utf-8"\r\n'
    b"Content-Transfer-Encoding: quoted-printable\r\n"
    b"\r\n"
    b'<html class="apple-mail-supports-explicit-dark-mode"><head>'
    b'<meta http-equiv="content-type" content="text/html; charset=utf-8">'
    b"</head><body dir=\"auto\">Yes. Please share.=C2=A0"
    b'<div><br></div><div dir="ltr"><div>Makeni Bent Digital Consulting</div>'
    b"</div><div>On Oct 13, 2026, at 19:28, Kawsar Mahmud "
    b"&lt;kawsar@example.com&gt; wrote:</div>"
    b"<blockquote type=\"cite\">original pitch text</blockquote>"
    b"</body></html>\r\n"
    b"--am1--\r\n"
)


# ── html_to_text ─────────────────────────────────────────────────────────────

def test_html_to_text_block_tags_and_entities():
    from app.services.imap_helpers import html_to_text

    text = html_to_text(
        "<html><body><p>Line one &amp; more</p><br><div>Line&nbsp;two</div>"
        "<script>evil()</script><style>p{}</style></body></html>"
    )
    assert "Line one & more" in text
    assert "Line two" in text
    assert "evil()" not in text
    assert "p{" not in text
    assert "<" not in text


def test_html_to_text_survives_broken_markup():
    from app.services.imap_helpers import html_to_text

    text = html_to_text("<p>unclosed <div <div>still readable</p>")
    assert "still readable" in text


# ── extract_email_body HTML fallback ─────────────────────────────────────────

def test_html_only_multipart_body_is_extracted():
    from app.services.imap_helpers import extract_email_body

    body = extract_email_body(_parse(HTML_ONLY_MULTIPART))
    assert "we are interested" in body
    assert "schedule a call" in body
    assert "<html" not in body


def test_empty_plain_part_falls_back_to_html():
    from app.services.imap_helpers import extract_email_body

    body = extract_email_body(_parse(EMPTY_PLAIN_PLUS_HTML))
    assert "Sounds good, tell me more." in body


def test_plain_part_still_wins_over_html():
    from app.services.imap_helpers import extract_email_body

    body = extract_email_body(_parse(PLAIN_AND_HTML))
    assert "Plain answer wins." in body
    assert "HTML answer loses." not in body


def test_single_part_html_is_converted_not_leaked():
    from app.services.imap_helpers import extract_email_body

    body = extract_email_body(_parse(SINGLE_PART_HTML))
    assert "Happy to connect next week." in body
    assert "<p>" not in body
    assert "color:red" not in body


def test_apple_mail_unclosed_meta_does_not_suppress_body():
    """Production regression: an unclosed <meta> in <head> emptied the body.

    Apple Mail replies carry a single quoted-printable text/html part whose
    head opens a void <meta> tag that never closes. The extractor must still
    return the reply text (and decode =C2=A0 to a space).
    """
    from app.services.imap_helpers import extract_email_body

    body = extract_email_body(_parse(APPLE_MAIL_HTML_ONLY))
    assert "Yes. Please share." in body
    assert "Makeni Bent Digital Consulting" in body
    assert "<html" not in body


def test_apple_mail_reply_strips_quoted_thread_and_classifies_positive():
    from app.services.imap_helpers import extract_email_body
    from app.services.imap_reply_tracker import extract_clean_reply, classify_sentiment

    msg = _parse(APPLE_MAIL_HTML_ONLY)
    reply = extract_clean_reply(extract_email_body(msg))
    assert reply.startswith("Yes. Please share.")
    assert "original pitch text" not in reply
    assert classify_sentiment(msg.get("Subject", ""), reply) == "positive"


def test_html_only_reply_classifies_from_recovered_text():
    from app.services.imap_helpers import extract_email_body
    from app.services.imap_reply_tracker import extract_clean_reply, classify_sentiment

    body = extract_email_body(_parse(HTML_ONLY_MULTIPART))
    reply = extract_clean_reply(body)
    assert reply.strip()
    assert classify_sentiment("Re: Question About Makeni Bent?", reply) == "positive"


# ── Repair pass for already-stored empty replies ─────────────────────────────

def _reply_log(tmp_path, monkeypatch, records):
    import app.services.reply_log as rl

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "reply_log.json")
    monkeypatch.setattr(rl, "PROCESSED_FILE", tmp_path / "reply_processed.json")
    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    rl.save_log(records)
    return rl


def test_repairable_record_ids_only_empty_bodies():
    from app.services.imap_reply_tracker import _repairable_record_ids

    ids = _repairable_record_ids([
        {"id": "a", "message_id": "<empty@x>", "full_body": ""},
        {"id": "b", "message_id": "<blank@x>", "full_body": "   \n "},
        {"id": "c", "message_id": "<full@x>", "full_body": "has text"},
        {"id": "d", "message_id": "", "full_body": ""},
    ])
    assert ids == {"<empty@x>", "<blank@x>"}


def test_collect_body_repairs_reextracts_html_only_message():
    from app.services.imap_reply_tracker import collect_body_repairs

    repairs = collect_body_repairs(
        [_parse(HTML_ONLY_MULTIPART)], {"<html-only@example.com>"}
    )
    fix = repairs["<html-only@example.com>"]
    assert "we are interested" in fix["full_body"]
    assert fix["sentiment"] == "positive"
    assert fix["body_preview"]


def test_collect_body_repairs_ignores_other_messages():
    from app.services.imap_reply_tracker import collect_body_repairs

    assert collect_body_repairs([_parse(HTML_ONLY_MULTIPART)], set()) == {}
    assert collect_body_repairs(
        [_parse(HTML_ONLY_MULTIPART)], {"<other@x>"}
    ) == {}


async def test_apply_body_repairs_updates_record_and_promotes_lead(
    tmp_path, monkeypatch
):
    from app.services.imap_reply_tracker import apply_body_repairs

    rl = _reply_log(tmp_path, monkeypatch, [
        {"id": "rep_1", "message_id": "<html-only@example.com>",
         "full_body": "", "body_preview": "", "sentiment": "question",
         "status": "unread"},
        {"id": "rep_2", "message_id": "<keep@x>", "full_body": "text",
         "sentiment": "question", "status": "archived"},
    ])

    changed = await apply_body_repairs({
        "<html-only@example.com>": {
            "full_body": "Yes, we are interested — send pricing!",
            "body_preview": "Yes, we are interested — send pricing!",
            "sentiment": "positive",
        },
    })

    assert changed == 1
    records = {r["id"]: r for r in rl.load_log()}
    assert "interested" in records["rep_1"]["full_body"]
    assert records["rep_1"]["sentiment"] == "positive"
    assert records["rep_1"]["status"] == "lead"
    # Untouched records keep their human-set status.
    assert records["rep_2"]["status"] == "archived"


async def test_apply_body_repairs_never_overwrites_human_status(
    tmp_path, monkeypatch
):
    from app.services.imap_reply_tracker import apply_body_repairs

    rl = _reply_log(tmp_path, monkeypatch, [
        {"id": "rep_1", "message_id": "<m@x>", "full_body": "",
         "sentiment": "question", "status": "archived"},
    ])
    await apply_body_repairs({
        "<m@x>": {"full_body": "interested", "body_preview": "interested",
                  "sentiment": "positive"},
    })
    assert rl.load_log()[0]["status"] == "archived"


async def test_apply_body_repairs_noop_without_repairs(tmp_path, monkeypatch):
    from app.services.imap_reply_tracker import apply_body_repairs

    _reply_log(tmp_path, monkeypatch, [{"id": "rep_1", "message_id": "<m@x>"}])
    assert await apply_body_repairs({}) == 0


# ── Re-tagging stored sentiments after classifier changes ────────────────────

async def test_reclassify_stored_sentiments_retags_polite_decline(tmp_path, monkeypatch):
    from app.services.imap_reply_tracker import reclassify_stored_sentiments

    decline = (
        "At this time, we are not exploring this type of collaboration, but we "
        "have saved your contact information and will certainly get in touch if "
        "the situation changes."
    )
    rl = _reply_log(tmp_path, monkeypatch, [
        {"id": "rep_1", "message_id": "<decline@x>", "subject": "Re: Proposal",
         "full_body": decline, "sentiment": "question", "status": "unread"},
        {"id": "rep_2", "message_id": "<empty@x>", "subject": "Re: Proposal",
         "full_body": "", "sentiment": "question", "status": "unread"},
        {"id": "rep_3", "message_id": "<warm@x>", "subject": "Re: Proposal",
         "full_body": "Yes please, happy to connect", "sentiment": "positive",
         "status": "lead"},
        {"id": "rep_4", "message_id": "<late@x>", "subject": "Re: Proposal",
         "full_body": "Sounds good, let's talk tomorrow", "sentiment": "question",
         "status": "unread"},
    ])

    changed = await reclassify_stored_sentiments()

    assert changed == 2
    records = {r["id"]: r for r in rl.load_log()}
    assert records["rep_1"]["sentiment"] == "negative"
    # A negative re-tag never demotes a status a human may have set.
    assert records["rep_1"]["status"] == "unread"
    # Empty bodies belong to the repair pass and stay untouched.
    assert records["rep_2"]["sentiment"] == "question"
    assert records["rep_3"]["sentiment"] == "positive"
    # A flip to positive mirrors ingestion's unread -> lead promotion.
    assert records["rep_4"]["sentiment"] == "positive"
    assert records["rep_4"]["status"] == "lead"


async def test_reclassify_stored_sentiments_noop_when_unchanged(tmp_path, monkeypatch):
    from app.services.imap_reply_tracker import reclassify_stored_sentiments

    rl = _reply_log(tmp_path, monkeypatch, [
        {"id": "rep_1", "message_id": "<m@x>", "subject": "Re: Offer",
         "full_body": "Not interested, unsubscribe", "sentiment": "negative",
         "status": "archived"},
    ])
    assert await reclassify_stored_sentiments() == 0
    assert rl.load_log()[0]["sentiment"] == "negative"


async def test_reclassify_demotes_auto_promoted_lead(tmp_path, monkeypatch):
    """A reply stored as positive only because the old classifier defaulted
    unknown bodies to positive ("hi") must lose the lead status too.

    Re-tagging the sentiment alone left the card green ("Hot Lead Flagged"),
    in the "Marked Lead" filter and counted as a lead forever — exactly the
    false positive a lone "hi" test mail produced.
    """
    from app.services.imap_reply_tracker import reclassify_stored_sentiments

    rl = _reply_log(tmp_path, monkeypatch, [
        {"id": "rep_1", "message_id": "<hi@x>", "subject": "Re: Hi",
         "full_body": "hi", "sentiment": "positive", "status": "lead"},
    ])

    assert await reclassify_stored_sentiments() == 1

    record = rl.load_log()[0]
    assert record["sentiment"] == "neutral"
    assert record["status"] == "unread"
    assert record["status_source"] == "auto"


async def test_reclassify_keeps_a_human_lead_status(tmp_path, monkeypatch):
    """A status a human set (``status_source: manual``) is never undone by a
    sentiment correction — only the classifier's own promotion is."""
    from app.services.imap_reply_tracker import reclassify_stored_sentiments

    rl = _reply_log(tmp_path, monkeypatch, [
        {"id": "rep_1", "message_id": "<hi@x>", "subject": "Re: Hi",
         "full_body": "hi", "sentiment": "positive", "status": "lead",
         "status_source": "manual"},
    ])

    assert await reclassify_stored_sentiments() == 1

    record = rl.load_log()[0]
    assert record["sentiment"] == "neutral"
    assert record["status"] == "lead"
    assert record["status_source"] == "manual"


def test_status_api_marks_the_change_as_manual(tmp_path, monkeypatch):
    """Manual status edits must outrank a later automatic correction."""
    rl = _reply_log(tmp_path, monkeypatch, [
        {"id": "rep_1", "message_id": "<hi@x>", "subject": "Re: Hi",
         "full_body": "hi", "sentiment": "positive", "status": "unread",
         "status_source": "auto"},
    ])

    assert rl.update_reply_status("rep_1", "lead") is True

    record = rl.load_log()[0]
    assert record["status"] == "lead"
    assert record["status_source"] == "manual"


# ── End-to-end: a scan repairs a stored empty reply ──────────────────────────

async def test_scan_repairs_stored_empty_reply(tmp_path, monkeypatch):
    """A processed HTML-only reply stored empty is repaired by the next scan."""
    import app.services.imap_reply_tracker as T
    import app.services.reply_log as rl
    from app.services.listmonk_client import ListMonkClient

    _reply_log(tmp_path, monkeypatch, [
        {"id": "rep_old", "message_id": "<apple-mail@example.com>",
         "from_email": "prospect@example.com", "full_body": "",
         "body_preview": "", "sentiment": "question", "status": "unread"},
    ])
    rl.mark_processed_message_ids(["<apple-mail@example.com>"])

    monkeypatch.setattr(T.settings, "imap_host", "imap.example.com")
    monkeypatch.setattr(T.settings, "imap_user", "me@example.com")
    monkeypatch.setattr(T.settings, "imap_pass", "secret")
    monkeypatch.setattr(T, "_fetch_campaigns_covering", _no_campaigns)
    monkeypatch.setattr(T, "_connect_imap", lambda: _FakeIMAP())

    client = ListMonkClient()
    stats = await T._scan_and_track_replies_impl(client, days_back=14)

    assert stats["repaired"] == 1
    assert stats["new_replies"] == 0  # not duplicated as a new reply
    record = rl.load_log()[0]
    assert "Yes. Please share." in record["full_body"]
    assert record["sentiment"] == "positive"
    assert record["status"] == "lead"


class _FakeIMAP:
    """Minimal IMAP stand-in serving one HTML-only message."""

    def select(self, *args, **kwargs):
        return "OK", [b"1"]

    def search(self, *args, **kwargs):
        return "OK", [b"1"]

    def fetch(self, num, spec):
        return "OK", [(b"1 (RFC822)", APPLE_MAIL_HTML_ONLY)]

    def close(self):
        pass

    def logout(self):
        pass


async def _no_campaigns(listmonk, cutoff):
    return [], True


# ── End-to-end: an inbox DSN becomes a bounce, never a reply ──────────────────

def _inbox_dsn_raw() -> bytes:
    import email.utils

    date = email.utils.format_datetime(
        email.utils.parsedate_to_datetime("Wed, 14 Oct 2026 14:10:50 +0200")
    )
    return (
        b"From: Mail Delivery System <mailer-daemon@host.getaevoramedia.com>\r\n"
        b"To: me@example.com\r\n"
        b"Subject: Mail delivery failed: returning message to sender\r\n"
        b"Message-ID: <inbox-dsn@example.com>\r\n"
        b"Date: " + date.encode() + b"\r\n"
        b"\r\n"
        b"This message was created automatically by mail delivery software.\r\n"
        b"\r\n"
        b"A message that you sent could not be delivered to one or more of its\r\n"
        b"recipients. This is a permanent error. The following address(es) failed:\r\n"
        b"\r\n"
        b"business@bandalabs.com\r\n"
        b"  SMTP error from remote mail server after RCPT TO:<business@bandalabs.com>:\r\n"
        b"  550-5.1.1 The email account that you tried to reach does not exist.\r\n"
    )


_HUMAN_REPLY_RAW = (
    b"From: Abdul Fattah Ismail <admin@makenibent.com>\r\n"
    b"To: me@example.com\r\n"
    b"Subject: Re: Question About Makeni Bent Digital Consulting LLC?\r\n"
    b"Message-ID: <yes-reply@example.com>\r\n"
    b"Date: Wed, 14 Oct 2026 13:00:00 +0000\r\n"
    b"\r\n"
    b"Yes. Please share.\r\n"
)


class _FakeListmonk:
    """ListMonk double backing both the DSN recipient and the reply sender."""

    def __init__(self):
        self.created = []

    async def get_subscribers(self, page, per_page, query):
        return {"data": {"results": [{
            "id": 42,
            "email": "business@bandalabs.com",
            "lists": [{"id": 1}],
        }], "total": 1}}

    async def create_bounce(self, **kwargs):
        self.created.append(kwargs)
        return {"data": True}


class _InboxIMAP:
    """Minimal IMAP stand-in serving a fixed set of raw messages."""

    def __init__(self, raw_messages: list[bytes]):
        self.raw_messages = raw_messages

    def select(self, *args, **kwargs):
        return "OK", [b"1"]

    def search(self, *args, **kwargs):
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.raw_messages)))]

    def fetch(self, num, spec):
        idx = int(num) - 1
        return "OK", [(num, self.raw_messages[idx])]

    def close(self):
        pass

    def logout(self):
        pass


async def _run_inbox_scan(
    tmp_path, monkeypatch, messages, campaign_lists=None, campaign_created_at=None,
):
    """Run a reply scan against a fake inbox and one fake campaign.

    The campaign date is anchored to the messages' own (fixed) October 2026
    dates rather than ``now``: attribution only considers campaigns created
    *on or before* the message, so a ``now - 1 day`` fixture stops covering
    the hardcoded Oct 14 messages as soon as the wall clock moves past them —
    which is exactly how this suite silently went red (and blocked deploys).
    """
    import app.services.imap_reply_tracker as T
    import app.services.bounce_ingest as bi
    from datetime import datetime, timezone

    rl = _reply_log(tmp_path, monkeypatch, [])
    monkeypatch.setattr(bi, "PROCESSED_FILE", tmp_path / "bounce_processed.json")
    monkeypatch.setattr(
        bi, "subscriber_opened_campaign",
        lambda *a, **k: _no_opened(),
    )
    monkeypatch.setattr(T.settings, "imap_host", "imap.example.com")
    monkeypatch.setattr(T.settings, "imap_user", "me@example.com")
    monkeypatch.setattr(T.settings, "imap_pass", "secret")
    monkeypatch.setattr(T.settings, "imap_use_ssl", False)

    created_at = campaign_created_at or datetime(2026, 1, 1, tzinfo=timezone.utc)
    lists = campaign_lists if campaign_lists is not None else [{"id": 1}]

    async def _campaigns(listmonk, cutoff):
        return [{
            "id": 7,
            "uuid": "camp-uuid-7",
            "name": "Cold Outreach Campaign",
            "created_at": created_at.isoformat(),
            "lists": lists,
        }], True

    monkeypatch.setattr(T, "_fetch_campaigns_covering", _campaigns)
    monkeypatch.setattr(T, "_connect_imap", lambda: _InboxIMAP(messages))

    fake = _FakeListmonk()
    stats = await T._scan_and_track_replies_impl(fake, days_back=14)
    return stats, fake, rl


async def _no_opened():
    return False


async def test_scan_routes_inbox_dsn_to_bounce_pipeline(tmp_path, monkeypatch):
    """A mailer-daemon DSN in the primary inbox is never stored as a reply:
    it becomes a ListMonk bounce record (Bounces page / Analytics counts)."""
    stats, fake, rl = await _run_inbox_scan(
        tmp_path, monkeypatch, [_inbox_dsn_raw(), _HUMAN_REPLY_RAW],
    )

    # The DSN stays out of the feed; the human reply is recorded normally.
    assert stats["bounce"] == 1
    assert stats["new_replies"] == 1
    records = rl.load_log()
    assert len(records) == 1
    assert records[0]["full_body"].strip() == "Yes. Please share."
    assert records[0]["sentiment"] == "positive"

    # ... and it was recorded through the bounce pipeline, hard (5.1.1).
    assert len(fake.created) == 1
    bounce = fake.created[0]
    assert bounce["email"] == "business@bandalabs.com"
    assert bounce["bounce_type"] == "hard"
    assert bounce["meta"]["ingested_by"] == "reply_tracker"

    # Idempotency: the reply scanner's processed set has both messages, and
    # the bounce ingester's set has the DSN so a shared mailbox never
    # double-records it.
    assert "<inbox-dsn@example.com>" in rl.load_processed_message_ids()
    assert "<yes-reply@example.com>" in rl.load_processed_message_ids()
    import app.services.bounce_ingest as bi
    assert bi.load_processed_ids() == ["<inbox-dsn@example.com>"]


async def test_scan_skips_dsn_already_ingested_by_bounce_scanner(tmp_path, monkeypatch):
    """When the bounce ingester already recorded the DSN (both scanners watch
    the same mailbox), the reply scan must not create a second bounce."""
    import app.services.bounce_ingest as bi

    # Point the shared bounce state at tmp before seeding it.
    monkeypatch.setattr(bi, "PROCESSED_FILE", tmp_path / "bounce_processed.json")
    bi.mark_processed_ids(["<inbox-dsn@example.com>"])
    stats, fake, rl = await _run_inbox_scan(
        tmp_path, monkeypatch, [_inbox_dsn_raw(), _HUMAN_REPLY_RAW],
    )

    assert stats["bounce"] == 1
    assert fake.created == []
    records = rl.load_log()
    assert [r["full_body"].strip() for r in records] == ["Yes. Please share."]
    assert "<inbox-dsn@example.com>" in rl.load_processed_message_ids()


# ── Unattributed mail must not masquerade as a campaign reply ────────────────

_HI_TEST_MAIL_RAW = (
    b"From: MD Kawser <md.kawser.drive2@gmail.com>\r\n"
    b"To: me@example.com\r\n"
    b"Subject: Re: Hi\r\n"
    b"Message-ID: <hi-test@example.com>\r\n"
    b"Date: Wed, 14 Oct 2026 09:24:49 +0600\r\n"
    b"Content-Type: text/plain; charset=\"utf-8\"\r\n"
    b"\r\n"
    b"hi\r\n"
)


async def test_scan_leaves_an_unattributed_reply_unlabelled(tmp_path, monkeypatch):
    """A test mail that matches no campaign is stored honestly.

    It used to be tagged with the fabricated campaign name "Cold Outreach
    Campaign" and, before the neutral default, hot-flagged as an interested
    lead simply because "hi" matched no keyword.
    """
    stats, _, rl = await _run_inbox_scan(
        tmp_path, monkeypatch, [_HI_TEST_MAIL_RAW],
        # A campaign on a list the sender is not subscribed to: neither the
        # subject nor the list-aware fallback can attribute this mail.
        campaign_lists=[{"id": 99}],
    )

    assert stats["new_replies"] == 1
    record = rl.load_log()[0]
    assert record["campaign_id"] is None
    assert record["campaign_name"] is None      # no invented campaign label
    assert record["sentiment"] == "neutral"     # "hi" carries no interest signal
    assert record["status"] == "unread"         # so it is not a lead
    assert record["status_source"] == "auto"


# ── Automated responses must not receive campaign attribution ────────────────

_TICKET_AUTO_REPLY_RAW = (
    b"From: FOREVER Support <support@forever.com>\r\n"
    b"To: me@example.com\r\n"
    b"Subject: Request received: Question About FOREVER ? - Ticket 514499\r\n"
    b"Message-ID: <ticket-auto@example.com>\r\n"
    b"Date: Wed, 14 Oct 2026 16:56:27 +0000\r\n"
    b"Content-Type: text/plain; charset=\"utf-8\"\r\n"
    b"\r\n"
    b"Hi Kawsar,\r\n"
    b"\r\n"
    b"Thank you for contacting us! Your request (514499) has been received\r\n"
    b"and is being reviewed by the FOREVER Client Care Team. A team member\r\n"
    b"will be in touch within 24-48 hours.\r\n"
)


async def test_scan_skips_campaign_matching_for_auto_replies(tmp_path, monkeypatch):
    """A ticket-system auto-response from a list member must not be attributed
    to the campaign that targeted that list — it is not genuine prospect
    engagement and would inflate per-campaign charts and reply rate."""
    stats, _, rl = await _run_inbox_scan(
        tmp_path, monkeypatch, [_TICKET_AUTO_REPLY_RAW],
        # The sender IS on list 1 (the campaign's target list), so the
        # list-aware fallback *would* match — but auto-replies skip matching.
        campaign_lists=[{"id": 1}],
    )

    assert stats["new_replies"] == 1
    assert stats["auto_reply"] == 1
    record = rl.load_log()[0]
    assert record["sentiment"] == "auto_reply"
    assert record["campaign_id"] is None       # no attribution despite list match
    assert record["campaign_name"] is None
    assert record["status"] == "unread"        # never promoted to lead
