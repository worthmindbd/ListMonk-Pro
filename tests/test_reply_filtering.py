"""Regressions for the cold-email inbox filtering review.

Covers the classifier (automated / interested / not interested / neutral),
quote stripping across mail clients and locales, campaign-echo stripping,
RFC 3834 header detection, the "is this actually a reply" scan gate, and the
unsubscribe scanner's echo backstop.
"""

import email.message
import email.policy
import json

import pytest

from app.services.imap_helpers import (
    automated_header_kind,
    campaign_echo_index,
    is_bulk_mail,
    strip_campaign_echo,
)
from app.services.imap_reply_tracker import (
    _auto_reply_kind,
    classify_sentiment,
    extract_clean_reply,
)
from app.services.imap_unsubscribe import KEYWORD_PATTERN, _extract_reply_only

SUBJ = "Re: Quick question"
HUMAN = "jane@acme.com"


# ── Classifier: real cold-outreach replies ───────────────────────────────────

@pytest.mark.parametrize("body", [
    "Sure, send over a deck.",
    "Sounds interesting. What's your availability next week?",
    "Let's do it. 2pm Tuesday works.",
    "Can you send me a proposal?",
    "Could we set up a meeting?",
    "Sure, why not. Call me at 555-1234.",
    "I've forwarded this to our marketing director.",
    "yep, send it over",
    "Are you free Thursday at 3?",
    "We'd love to see some case studies.",
    # An explicit request beats a soft decline in the same reply.
    "I don't need a demo, just send pricing.",
])
def test_interested_replies_are_positive(body):
    assert classify_sentiment(SUBJ, body, HUMAN) == "positive"


@pytest.mark.parametrize("body", [
    "No thank you.",
    "No.",
    "Nope",
    "Nah",
    "Not for us, thanks.",
    "We're all set, thanks.",
    "We already have a vendor for this.",
    "We already work with an agency for this, thanks.",
    "Please stop sending me these.",
    "Stop spamming me",
    "Please don't contact me again.",
    "We don't have budget for this.",
    "This is spam. How did you get my email?",
    "I don't think this is a good fit.",
    "We'll pass, thanks.",
])
def test_declines_are_negative(body):
    assert classify_sentiment(SUBJ, body, HUMAN) == "negative"


@pytest.mark.parametrize("body", [
    "What company is this?",
    "I'll get back to you next week.",
    "Received, thanks.",
    # "all set for" is a confirmation, not a decline.
    "We're all set for Tuesday's call, thanks!",
])
def test_ambiguous_replies_stay_neutral(body):
    assert classify_sentiment(SUBJ, body, HUMAN) in ("neutral", "positive")
    assert classify_sentiment(SUBJ, body, HUMAN) != "negative"


@pytest.mark.parametrize("subject,body,sender", [
    (SUBJ, "Thank you for your email. I am currently travelling with limited access to email.", HUMAN),
    (SUBJ, "Thanks for contacting us! We've received your message and will respond within 24 hours.", "support@acme.com"),
    (SUBJ, "Thank you for reaching out. This mailbox is not monitored.", "info@acme.com"),
    (SUBJ, "I am no longer with Acme. Please contact sales@acme.com.", HUMAN),
    ("Auto: Quick question", "Ich bin bis 10.11 nicht im Büro.", "hans@acme.de"),
    ("Réponse automatique", "Je suis absente jusqu'au lundi.", "marie@acme.fr"),
    ("Automatische Antwort: Partnership", "Vielen Dank für Ihre Nachricht.", "hans@x.de"),
    (SUBJ, "Thanks for your email, we'll get back to you shortly.", "hello@acme.com"),
    # Vacation notices routinely contain "call me" / "schedule" / "pricing":
    # those are not interest and must not override the away notice.
    (SUBJ, "I am currently out of the office and will return on 12 November. "
           "For urgent matters please call me on +44 7700 900000.", HUMAN),
    (SUBJ, "I'm on annual leave until Monday. To schedule a meeting, please contact "
           "my assistant. For pricing questions contact sales@x.com.", HUMAN),
    (SUBJ, "Thanks for reaching out. Your email has been forwarded to our sales team "
           "who will get back to you within 2 business days.", "info@x.com"),
])
def test_automated_replies_are_auto_reply(subject, body, sender):
    assert classify_sentiment(subject, body, sender) == "auto_reply"


@pytest.mark.parametrize("body", [
    "Interested! I'm on vacation till Friday, ping me Monday.",
    "Thanks! I'm away from my desk but very interested.",
    "I'm on leave this week, but yes, let's schedule a call for Monday.",
    "Hi! Out of office this week, but this sounds great — let's set up a call next Tuesday.",
])
def test_interested_human_mentioning_absence_is_not_auto_reply(body):
    """Regression: these were tagged auto_reply (no campaign attribution, out
    of the reply rate) and blocklisted with the OOO toggle on."""
    assert classify_sentiment(SUBJ, body, HUMAN) == "positive"
    assert _auto_reply_kind(SUBJ, body, HUMAN) is None


@pytest.mark.parametrize("body", [
    "Request received, I'll review it this week and get back to you.",
    "We use Zendesk today — could yours replace it? Interested.",
    "Our help desk ticket system is a pain, let's talk.",
])
def test_human_mentioning_helpdesk_words_is_not_machine(body):
    """Regression: the 'safe' machine toggle blocklisted these humans."""
    assert _auto_reply_kind(SUBJ, body, HUMAN) is None
    assert classify_sentiment(SUBJ, body, HUMAN) != "auto_reply"


def test_header_verdict_is_decisive():
    # A vacation responder carrying interest words is still a responder.
    assert classify_sentiment(SUBJ, "Interested? I'll reply on return.", HUMAN, "auto_replied") == "auto_reply"
    assert _auto_reply_kind(SUBJ, "Back on Monday.", HUMAN, "auto_replied") == "ooo"
    assert _auto_reply_kind(SUBJ, "We have received your message.", HUMAN, "auto_replied") == "machine"
    assert _auto_reply_kind(SUBJ, "Your weekly report is ready", "reports@x.com", "auto_generated") == "machine"


@pytest.mark.parametrize("sender,expected", [
    ("messages-noreply@linkedin.com", "auto_reply"),
    ("drive-shares-dm-noreply@google.com", "auto_reply"),
    ("calendar-notification@google.com", "auto_reply"),
    ("noreply@shop.example", "auto_reply"),
    ("agent@acme.zendesk.com", "auto_reply"),
    # Human-looking role addresses are no longer machines.
    ("news.editor@paper.com", "positive"),
    ("alerts-team@acme.com", "positive"),
])
def test_automated_sender_detection(sender, expected):
    assert classify_sentiment("Hello", "Interested, tell me more.", sender) == expected


# ── Quote stripping ──────────────────────────────────────────────────────────

FOOTER = "Hi Jane, if you're interested, let's talk. Not a fit? Just reply \"remove me\"."


@pytest.mark.parametrize("quoted", [
    f"From: Sam Sender\nSent: Monday, November 3, 2025 10:00 AM\nTo: Jane\nSubject: Quick question\n\n{FOOTER}",
    f"________________________________\nFrom: Sam Sender\nSent: Monday\n{FOOTER}",
    f"Le lun. 3 nov. 2025 à 10:00, Sam <sam@x.com> a écrit :\n{FOOTER}",
    f"Am Mo., 3. Nov. 2025 um 10:00 Uhr schrieb Sam <sam@x.com>:\n{FOOTER}",
    f"El lun, 3 nov 2025 a las 10:00, Sam (<sam@x.com>) escribió:\n{FOOTER}",
    f"Sam <sam@x.com> schrieb am Mo., 3. Nov. 2025, 10:00:\n{FOOTER}",
    f"Von: Sam Sender\nGesendet: Montag, 3. November 2025\nAn: Jane\n{FOOTER}",
    f"De : Sam Sender\nEnvoyé : lundi 3 novembre 2025\nÀ : Jane\n{FOOTER}",
    f"-----Ursprüngliche Nachricht-----\n{FOOTER}",
    f"Sent from my iPhone\n\n{FOOTER}",
    f"--\nJane Doe | CTO\nTo unsubscribe from our newsletter click here\n{FOOTER}",
])
def test_quoted_campaign_copy_is_stripped(quoted):
    body = f"Thanks, will review.\n\n{quoted}"
    reply = extract_clean_reply(body)
    assert reply == "Thanks, will review."
    assert classify_sentiment(SUBJ, reply, HUMAN) == "neutral"


def test_outlook_reply_never_triggers_unsubscribe_keyword():
    """The high-severity regression: an Outlook "Thanks" to a campaign whose
    footer says reply "remove me" was auto-unsubscribed."""
    body = (
        "Thanks, will review.\n\nFrom: Sam Sender\n"
        "Sent: Monday, November 3, 2025 10:00 AM\nTo: Jane\n"
        "Subject: Quick question\n\nHi Jane... Not the right fit? Just reply "
        "\"remove me\" and I will not follow up."
    )
    assert not KEYWORD_PATTERN.search(_extract_reply_only(body))


def test_genuine_inline_text_is_kept():
    # "My boss wrote:" is the prospect's own sentence, not an attribution.
    body = "My boss wrote:\nwe should talk. Interested!"
    assert "Interested" in _extract_reply_only(body)


# ── Campaign echo stripping ──────────────────────────────────────────────────

CAMPAIGN_HTML = (
    "<p>Hi {{ .Subscriber.FirstName }}, if you're interested in growing your "
    "pipeline, let's talk this week.</p>"
    "<p>Not the right fit? Just reply 'remove me' and I will not follow up.</p>"
)
TEMPLATE_HTML = '<div>{{ template "content" . }}</div><p><a href="{{ UnsubscribeURL }}">Unsubscribe</a></p>'


def test_echo_strips_personalised_and_rewrapped_copy():
    index = campaign_echo_index(CAMPAIGN_HTML, TEMPLATE_HTML)
    reply = (
        "Thanks, will review.\n"
        "Unknown client header\n"
        "Hi Jane, if you're interested in growing your pipeline, let's talk this week.\n"
        "Not the right fit? Just reply 'remove me' and I\n"
        "will not follow up.\n"
        "Unsubscribe"
    )
    cleaned = strip_campaign_echo(reply, index)
    assert "remove me" not in cleaned
    assert "interested" not in cleaned
    assert "Unsubscribe" not in cleaned
    assert cleaned.startswith("Thanks, will review.")


def test_echo_keeps_a_one_word_unsubscribe_answer():
    """Footer link text 'Unsubscribe' must not erase a prospect who replies
    with exactly that word."""
    index = campaign_echo_index(CAMPAIGN_HTML, TEMPLATE_HTML)
    assert strip_campaign_echo("Unsubscribe", index) == "Unsubscribe"
    assert strip_campaign_echo("Please remove me", index) == "Please remove me"


# ── Headers ──────────────────────────────────────────────────────────────────

def _msg(**headers):
    m = email.message.EmailMessage()
    m["From"] = HUMAN
    m["Subject"] = SUBJ
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content("hello")
    return m


def test_automated_header_kind():
    assert automated_header_kind(_msg(Auto_Submitted="auto-replied")) == "auto_replied"
    assert automated_header_kind(_msg(Auto_Submitted="auto-generated")) == "auto_generated"
    assert automated_header_kind(_msg(Auto_Submitted="no")) is None
    assert automated_header_kind(_msg(X_Autoreply="yes")) == "auto_replied"
    assert automated_header_kind(_msg(Precedence="auto_reply")) == "auto_replied"
    assert automated_header_kind(_msg()) is None


def test_bulk_mail_detection():
    assert is_bulk_mail(_msg(List_Unsubscribe="<mailto:u@x.com>"))
    assert is_bulk_mail(_msg(Precedence="bulk"))
    # A list-headed message that answers ours is not a newsletter.
    assert not is_bulk_mail(_msg(List_Id="desk.x.com", In_Reply_To="<camp@x>"))
    assert not is_bulk_mail(_msg())


# ── Scan gate ────────────────────────────────────────────────────────────────

def _raw(sender, subject, body, msg_id, extra=""):
    return (
        f"From: {sender}\r\nTo: me@example.com\r\nSubject: {subject}\r\n"
        f"Date: Wed, 14 Oct 2026 10:00:00 +0000\r\nMessage-ID: {msg_id}\r\n"
        f"{extra}\r\n{body}\r\n"
    ).encode()


class _InboxIMAP:
    def __init__(self, raw_messages):
        self.raw_messages = raw_messages

    def select(self, *a, **k):
        return "OK", [b"1"]

    def search(self, *a, **k):
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.raw_messages)))]

    def fetch(self, num, spec):
        return "OK", [(num, self.raw_messages[int(num) - 1])]

    def close(self):
        pass

    def logout(self):
        pass


class _LM:
    """Only prospect@example.com is a subscriber; lookups can be made to fail."""

    def __init__(self, fail=False, campaign_body=None):
        self.fail = fail
        self.campaign_body = campaign_body
        self.lookups = 0

    async def get_subscribers(self, page, per_page, query):
        self.lookups += 1
        if self.fail:
            raise RuntimeError("listmonk down")
        if "prospect@example.com" in query:
            return {"data": {"results": [{"id": 42, "lists": [{"id": 1}]}]}}
        return {"data": {"results": []}}

    async def get_campaign(self, cid):
        return {"data": {"id": cid, "body": self.campaign_body or ""}}

    async def get_template(self, tid):
        return {"data": {"body": ""}}


async def _scan(tmp_path, monkeypatch, messages, lm, campaigns=None, settings_patch=None):
    import app.services.bounce_ingest as bi
    import app.services.imap_reply_tracker as T
    import app.services.reply_log as rl

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "reply_log.json")
    monkeypatch.setattr(rl, "PROCESSED_FILE", tmp_path / "reply_processed.json")
    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    monkeypatch.setattr(bi, "PROCESSED_FILE", tmp_path / "bounce_processed.json")
    if settings_patch:
        rl.save_settings(settings_patch)
    monkeypatch.setattr(T.settings, "imap_host", "imap.example.com")
    monkeypatch.setattr(T.settings, "imap_user", "me@example.com")
    monkeypatch.setattr(T.settings, "imap_pass", "secret")

    async def _campaigns(listmonk, cutoff):
        return list(campaigns or []), True

    monkeypatch.setattr(T, "_fetch_campaigns_covering", _campaigns)
    monkeypatch.setattr(T, "_connect_imap", lambda: _InboxIMAP(messages))
    stats = await T._scan_and_track_replies_impl(lm, days_back=14)
    return stats, rl


@pytest.mark.asyncio
async def test_scan_skips_mail_that_is_not_a_reply(tmp_path, monkeypatch):
    msgs = [
        _raw("prospect@example.com", "Re: Hello", "Interested, tell me more.", "<a@x>"),
        _raw("invoice+statements@stripe.com", "Your receipt", "Thanks — interested in more? Yes!", "<b@x>"),
        _raw("digest@news.example", "Weekly digest", "Interested in what's new?", "<c@x>",
             "List-Unsubscribe: <mailto:u@news.example>\r\n"),
    ]
    stats, rl = await _scan(tmp_path, monkeypatch, msgs, _LM())
    records = rl.load_log()
    assert [r["from_email"] for r in records] == ["prospect@example.com"]
    assert stats["not_reply"] == 2
    # Terminal outcome: skipped mail is not re-examined every scan.
    processed = json.loads((tmp_path / "reply_processed.json").read_text())
    assert {"<a@x>", "<b@x>", "<c@x>"} <= set(processed)


@pytest.mark.asyncio
async def test_scan_keeps_non_subscriber_answering_a_campaign_subject(tmp_path, monkeypatch):
    """A colleague the prospect forwarded to replies from another address."""
    campaigns = [{"id": 7, "name": "Pipeline", "subject": "Quick question about growth",
                  "created_at": "2026-01-01T00:00:00+00:00", "lists": [{"id": 1}], "body": ""}]
    msgs = [_raw("colleague@other.com", "Re: Quick question about growth", "Let's talk.", "<d@x>")]
    stats, rl = await _scan(tmp_path, monkeypatch, msgs, _LM(), campaigns)
    [record] = rl.load_log()
    assert record["campaign_id"] == 7 and record["sentiment"] == "positive"


@pytest.mark.asyncio
async def test_scan_defers_when_listmonk_lookup_fails(tmp_path, monkeypatch):
    msgs = [_raw("prospect@example.com", "Re: Hello", "Interested.", "<e@x>")]
    stats, rl = await _scan(tmp_path, monkeypatch, msgs, _LM(fail=True))
    assert stats["deferred"] == 1 and rl.load_log() == []
    assert not (tmp_path / "reply_processed.json").exists()


@pytest.mark.asyncio
async def test_scan_uses_auto_submitted_header(tmp_path, monkeypatch):
    msgs = [_raw("prospect@example.com", "Re: Hello", "Interested? Back Monday.", "<f@x>",
                 "Auto-Submitted: auto-replied\r\n")]
    stats, rl = await _scan(tmp_path, monkeypatch, msgs, _LM())
    [record] = rl.load_log()
    assert record["sentiment"] == "auto_reply" and record["auto_header"] == "auto_replied"
    assert record["status"] == "unread"


@pytest.mark.asyncio
async def test_scan_strips_campaign_echo_before_classifying(tmp_path, monkeypatch):
    """Unknown quote header: the campaign copy must not decide the sentiment."""
    body = "Not the right fit? Just reply 'remove me' and I will not follow up. Unsubscribe anytime."
    campaigns = [{"id": 9, "name": "Outreach", "subject": "Growth idea",
                  "created_at": "2026-01-01T00:00:00+00:00", "lists": [{"id": 1}],
                  "body": f"<p>{body}</p>"}]
    msgs = [_raw("prospect@example.com", "Re: Growth idea",
                 f"Thanks, will review.\r\n\r\n=== odd client header ===\r\n{body}", "<g@x>")]
    stats, rl = await _scan(tmp_path, monkeypatch, msgs, _LM(), campaigns)
    [record] = rl.load_log()
    assert record["sentiment"] == "neutral"
    assert "remove me" not in record["full_body"]


@pytest.mark.asyncio
async def test_auto_tag_leads_off_keeps_positive_unread(tmp_path, monkeypatch):
    msgs = [_raw("prospect@example.com", "Re: Hello", "Interested, send pricing.", "<h@x>")]
    stats, rl = await _scan(tmp_path, monkeypatch, msgs, _LM(),
                            settings_patch={"auto_tag_leads": False})
    [record] = rl.load_log()
    assert record["sentiment"] == "positive" and record["status"] == "unread"


# ── Stored records ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reclassify_restrips_outlook_quotes_and_restores_attribution(tmp_path, monkeypatch):
    import app.services.imap_reply_tracker as T
    import app.services.reply_log as rl

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "reply_log.json")
    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    rl.save_log([
        # Stored by an older version: Outlook quote kept, read as negative.
        {"id": "rep_1", "message_id": "<1@x>", "from_email": "prospect@example.com",
         "subject": "Re: Growth idea", "date": "Wed, 14 Oct 2026 10:00:00 +0000",
         "full_body": "Thanks\n\nFrom: Sam\nSent: Monday, November 3\nTo: Jane\n\nReply 'unsubscribe' to opt out.",
         "sentiment": "negative", "status": "unread", "campaign_id": None},
        # Tagged auto_reply by the old OOO rule; really a hot lead.
        {"id": "rep_2", "message_id": "<2@x>", "from_email": "prospect@example.com",
         "subject": "Re: Growth idea", "date": "Wed, 14 Oct 2026 10:00:00 +0000",
         "full_body": "Interested! I'm on vacation till Friday, ping me Monday.",
         "sentiment": "auto_reply", "status": "unread", "campaign_id": None},
    ])
    pending: list = []
    assert await T.reclassify_stored_sentiments(reattribute=pending) == 2
    r1, r2 = rl.load_log()
    assert r1["sentiment"] == "neutral" and r1["full_body"] == "Thanks"
    assert r2["sentiment"] == "positive" and r2["status"] == "lead"
    assert pending == ["rep_2"]

    campaigns = [{"id": 9, "name": "Outreach", "subject": "Growth idea",
                  "created_at": "2026-01-01T00:00:00+00:00", "lists": [{"id": 1}]}]
    assert await T.reattribute_replies(_LM(), pending, campaigns) == 1
    assert rl.load_log()[1]["campaign_id"] == 9


def test_retired_lead_tag_setting_is_dropped(tmp_path, monkeypatch):
    import app.services.reply_log as rl

    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    (tmp_path / "reply_settings.json").write_text(json.dumps({"lead_tag": "x", "auto_tag_leads": False}))
    loaded = rl.load_settings()
    assert "lead_tag" not in loaded and loaded["auto_tag_leads"] is False


def test_reply_scan_interval_reads_setting(tmp_path, monkeypatch):
    import app.main as main
    import app.services.reply_log as rl

    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    assert main._reply_scan_interval() == main.REPLY_SCAN_INTERVAL
    rl.save_settings({"scan_interval_minutes": 5})
    assert main._reply_scan_interval() == 300
    rl.save_settings({"scan_interval_minutes": "junk"})
    assert main._reply_scan_interval() == main.REPLY_SCAN_INTERVAL


# ── Unsubscribe scanner: echo backstop ───────────────────────────────────────

class _UnsubIMAP:
    def __init__(self, raw):
        self.raw = raw
        self.flags = {}

    def select(self, mailbox):
        return ("OK", [b"1"])

    def search(self, charset, criteria):
        if criteria.startswith("(SEEN "):
            return ("OK", [b""])
        return ("OK", [b"1"])

    def fetch(self, msg_id, spec):
        return ("OK", [(b"1 (RFC822)", self.raw)])

    def store(self, msg_id, op, flags):
        self.flags[msg_id] = flags
        return ("OK", [b""])

    def logout(self):
        return ("BYE", [b""])


@pytest.mark.asyncio
async def test_unsubscribe_scan_ignores_keyword_in_quoted_campaign_copy(tmp_path, monkeypatch):
    import app.services.unsubscribe_log as shared_log
    from app.services import imap_unsubscribe as svc
    from app.services.listmonk_client import ListMonkClient

    for name in ("LOG_FILE", "SETTINGS_FILE", "PROCESSED_FILE", "EXAMINED_FILE"):
        monkeypatch.setattr(shared_log, name, tmp_path / f"{name}.json")

    footer = "Not the right fit? Just reply 'remove me' and I will not follow up."
    msg = email.message.EmailMessage()
    msg["From"] = "user@example.com"
    msg["Subject"] = "Re: Growth idea"
    msg["Message-ID"] = "<abc@example.com>"
    msg["Date"] = "Mon, 01 Sep 2025 12:00:00 +0000"
    msg.set_content(f"Thanks, will review.\n\n=== odd client header ===\n{footer}")
    monkeypatch.setattr(svc, "connect_imap", lambda: _UnsubIMAP(msg.as_bytes()))

    modify_calls = []

    async def mock_request(method, path, **kwargs):
        if method == "GET" and path == "/api/campaigns":
            return {"data": {"results": [{
                "id": 10, "name": "September", "created_at": "2025-09-01",
                "lists": [{"id": 1}], "body": f"<p>{footer}</p>",
            }], "total": 1}}
        if method == "GET" and "/api/subscribers" in path:
            return {"data": {"results": [{"id": 5, "email": "user@example.com",
                                          "name": "User", "lists": [{"id": 1}]}], "total": 1}}
        if method == "PUT" and "/api/subscribers/lists" in path:
            modify_calls.append(path)
        return {"data": {}}

    client = ListMonkClient.__new__(ListMonkClient)
    client._client = None
    client._request = mock_request

    result = await svc.scan_and_unsubscribe(client)
    assert result["processed"] == 0
    assert modify_calls == []


@pytest.mark.asyncio
async def test_echo_only_reply_stays_neutral_after_reclassification(tmp_path, monkeypatch):
    """A reply that is nothing but our quoted copy must not flip back to the
    footer's sentiment when the stored record is re-classified."""
    import app.services.imap_reply_tracker as T

    footer = "Not the right fit? Just reply 'remove me' and I will not follow up. Unsubscribe anytime."
    campaigns = [{"id": 9, "name": "Outreach", "subject": "Growth idea",
                  "created_at": "2026-01-01T00:00:00+00:00", "lists": [{"id": 1}],
                  "body": f"<p>{footer}</p>"}]
    msgs = [_raw("prospect@example.com", "Re: Growth idea", footer, "<i@x>")]
    stats, rl = await _scan(tmp_path, monkeypatch, msgs, _LM(), campaigns)
    [record] = rl.load_log()
    assert record["sentiment"] == "neutral" and record["echo_only"] is True
    assert record["full_body"].strip()  # never stored empty (repair loop)

    await T.reclassify_stored_sentiments()
    assert rl.load_log()[0]["sentiment"] == "neutral"
