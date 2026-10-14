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


# ── End-to-end: a scan repairs a stored empty reply ──────────────────────────

async def test_scan_repairs_stored_empty_reply(tmp_path, monkeypatch):
    """A processed HTML-only reply stored empty is repaired by the next scan."""
    import app.services.imap_reply_tracker as T
    import app.services.reply_log as rl
    from app.services.listmonk_client import ListMonkClient

    _reply_log(tmp_path, monkeypatch, [
        {"id": "rep_old", "message_id": "<html-only@example.com>",
         "from_email": "prospect@example.com", "full_body": "",
         "body_preview": "", "sentiment": "question", "status": "unread"},
    ])
    rl.mark_processed_message_ids(["<html-only@example.com>"])

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
    assert "we are interested" in record["full_body"]
    assert record["sentiment"] == "positive"
    assert record["status"] == "lead"


class _FakeIMAP:
    """Minimal IMAP stand-in serving one HTML-only message."""

    def select(self, *args, **kwargs):
        return "OK", [b"1"]

    def search(self, *args, **kwargs):
        return "OK", [b"1"]

    def fetch(self, num, spec):
        return "OK", [(b"1 (RFC822)", HTML_ONLY_MULTIPART)]

    def close(self):
        pass

    def logout(self):
        pass


async def _no_campaigns(listmonk, cutoff):
    return [], True
