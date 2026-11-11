"""Regressions from auditing the classifier against a live cold-outreach mailbox.

Every case in ``REAL_WORLD`` is a reply that was mis-filed in production
(senders anonymised, wording kept): two helpdesk/mass-mail auto-replies sat in
the feed as *Hot Leads*, a Polish "not interested" was *neutral*, and five
shared-inbox acknowledgements in four languages were ordinary replies. The
rest pin the behaviour around them:

* the multilingual vocabulary (``reply_i18n``) — recall for DE/FR/ES/IT/PL/DK…
* the precision guard — a wrong "automated" tag can blocklist a real person
  and a wrong "positive" is a fake Hot Lead, so the dangerous look-alikes
  (vacation notices that say "call me", footers that say "if you are
  interested") are asserted to stay what they are
* scan behaviour: threaded replies from non-subscribers are kept, old records
  are judged by their automation headers (also when the message is older than
  the scan window), stale campaign tags are cleared
* the unsubscribe scanner's opt-out vocabulary (English + other locales) and
  its refusal to act on automated mail
"""

import email
import email.message
import imaplib
import json

import pytest

from app.services.imap_reply_tracker import (
    HEADER_LOOKUP_LIMIT,
    _auto_reply_kind,
    _header_backfill_ids,
    classify_sentiment,
    collect_header_backfills,
    lookup_header_verdicts,
)
from app.services.imap_unsubscribe import KEYWORD_PATTERN

H = "jane@acme.com"
SUBJ = "Re: Question About Acme?"


def _id(value):
    return str(value)[:48].replace("\n", " ")


# ── Real mis-filed replies ───────────────────────────────────────────────────

REAL_WORLD = [
    # (expected, subject, body, sender, header_kind)
    # Helpdesk ticket acknowledgement: "we would love to hear them" made it a Hot Lead.
    ("auto_reply", "Request #192862 Question About Vitamin T?",
     "### Your incident was received Incident [#192862] ###\n\nReply above this line to add a comment\n\n"
     "Thank you for contacting Acme Help!\n\n- ADDING NOTES: If you have any updates for us regarding "
     "your issue, we would love to hear them! Please reply directly to this email.\n\n"
     "We will review your inquiry, and work with you to resolve it as quickly as possible.",
     "acme-help@vendor.example", None),
    # Mass-mail auto-reply addressed to "Dear Interested Party" — also a Hot Lead.
    ("auto_reply", "Thank you for your interest.",
     "Dear Interested Party: Thank you for contacting Gorilla Productions. We appreciate your interest "
     "in our company. There is no need to reply, this is a friendly *automated* message. One of our "
     "staff members will get back to you as quickly as possible.",
     "info@studio.example", None),
    # Shared inquiry desk, no auto-submitted header.
    ("auto_reply", "VISION inquiry",
     "Hello,\nThank you for inquiring about our production services. These emails are answered in "
     "under 24 hours. For an immediate response, please dial +1 555 0100. (This is a voice only landline.)",
     "inquire@films.example", None),
    # Dead address with a redirect.
    ("auto_reply", "Re: [SPAM] Question About PARIS?",
     "Hello this email address is no longer in service. Please use the email address bellow in order "
     "to contact the team.\n\ncontact (at) paris (.) fr\n\nBonjour cette adresse email n'est plus en "
     "service. Merci d'utiliser l'adresse email ci-dessous.",
     "booking@paris.example", None),
    # Spanish acknowledgement.
    ("auto_reply", "Gracias por comunicarte con Attittud",
     "Estimado/a,\n\nGracias por contactarte. Te responderemos a la brevedad posible.\n\n"
     "Si deseas enviar tu CV, por favor hazlo mediante este enlace.\n\nAgradecemos tu contacto y tu interés.",
     "info@consulting.example", None),
    # Italian "this is an automatic mail".
    ("auto_reply", "Richiesta in lavorazione",
     "Gentile cliente,\n\ngrazie per averci contattato. La sua richiesta è in fase di lavorazione.\n\n"
     "Riceverà una risposta nel più breve tempo possibile.\n\n------\n\nQuesta è una mail automatica.",
     "info@design.example", None),
    # Header-only auto-responder: nothing in the text gives it away.
    ("auto_reply", "Inscripcion Models",
     "Gracias por comunicarte con nosotros.\n\nSi ya has completado el registro web con todos tus datos "
     "entonces estaremos completando tu ficha.\n\nMuchas gracias.",
     "inscripcion@models.example", "auto_replied"),
    # The genuine ones must stay genuine.
    ("positive", "Re: Question About Makeni Bent?", "Yes. Please share.\n\nThe Admin | 917 555 0100", "admin@firm.example", None),
    ("negative", "Re: Question About Rollerads?",
     "Hello! Thank you very much for reaching out and sharing your proposal with us. At this time, we are "
     "not exploring this type of collaboration, but we have saved your contact information and will "
     "certainly get in touch if the situation changes.", "support@ads.example", None),
    ("negative", "Re: Question About Projektowanie?", "Dziekuje nie jestem zainteresowany", "kontakt@firma.example", None),
    ("neutral", "Re: Pixel Pulse", "Who’s this? Kind Regards, Tevyn\nOwner/Videographer", "owner@gmail.com", None),
]


@pytest.mark.parametrize("expected,subject,body,sender,header_kind", REAL_WORLD,
                         ids=[_id(c[1]) for c in REAL_WORLD])
def test_real_world_replies(expected, subject, body, sender, header_kind):
    assert classify_sentiment(subject, body, sender, header_kind) == expected


# ── English recall: opt-outs written for a company, soft declines, interest ──

@pytest.mark.parametrize("body", [
    "Please remove us from your list.",
    "Take us off your mailing list please.",
    "Remove us from your list",
    "STOP",
    "Unsubscribe",
    "Thanks but we're good.",
    "Not at this time, thanks.",
    "Not right now.",
    "We handle everything in-house.",
    "We have an in-house team for this.",
    "Not a priority for us this year.",
    "Interested? No. Please stop.",
    "Please delete our data.",
    "We will opt out of these emails.",
], ids=_id)
def test_english_not_interested(body):
    assert classify_sentiment(SUBJ, body, H) == "negative"


@pytest.mark.parametrize("body", [
    "This looks interesting, can you share a few examples of your work?",
    "Can we jump on a call tomorrow?",
    "Thanks for reaching out - we are actually looking for someone. Do you have availability next month?",
    "Hi, I'd like to know more about your services.",
    "We would love to hear more about what you offer.",
    # An explicit request beats a soft decline in the same reply.
    "Not right now, though I'd like to know more. Can you send examples?",
    "We're not currently looking, but send over your pricing and I'll keep it on file.",
], ids=_id)
def test_english_interested(body):
    assert classify_sentiment(SUBJ, body, H) == "positive"


@pytest.mark.parametrize("body", [
    "Who is this?",
    "Can you clarify what you are offering?",
    "Please contact my colleague Sam.",
    "Out of curiosity, what's your turnaround?",
    "Which of our locations were you looking at?",
], ids=_id)
def test_english_neutral(body):
    assert classify_sentiment(SUBJ, body, H) == "neutral"


# ── Multilingual vocabulary ──────────────────────────────────────────────────

I18N = [
    # German
    ("negative", "Kein Interesse, danke.", "mail@firma.de"),
    ("negative", "Wir haben leider keinen Bedarf.", "mail@firma.de"),
    ("negative", "Bitte keine weiteren E-Mails senden.", "mail@firma.de"),
    ("negative", "Bitte entfernen Sie mich aus Ihrem Verteiler.", "mail@firma.de"),
    ("negative", "Wir sind nicht interessiert.", "mail@firma.de"),
    ("negative", "Vielen Dank für Ihre Nachricht. Leider kein Interesse.", "mail@firma.de"),
    ("positive", "Das klingt interessant, senden Sie uns bitte mehr Informationen.", "mail@firma.de"),
    ("positive", "Wir haben Interesse. Können wir telefonieren?", "mail@firma.de"),
    ("auto_reply", "Dies ist eine automatische Antwort. Wir haben Ihre Nachricht erhalten.", "info@firma.de"),
    ("auto_reply", "Vielen Dank für Ihre Nachricht, wir melden uns schnellstmöglich.", "info@firma.de"),
    # French
    ("negative", "Merci, mais nous ne sommes pas intéressés.", "contact@societe.fr"),
    ("negative", "Ne m'envoyez plus de mails s'il vous plaît.", "contact@societe.fr"),
    ("negative", "Non merci.", "contact@societe.fr"),
    ("positive", "Ça m'intéresse, pouvez-vous m'envoyer plus d'informations ?", "contact@societe.fr"),
    ("positive", "Nous sommes intéressés, appelez-moi demain.", "contact@societe.fr"),
    ("auto_reply", "Ceci est un message automatique. Nous avons bien reçu votre demande.", "contact@societe.fr"),
    ("auto_reply", "Cette adresse email n'est plus en service.", "booking@societe.fr"),
    # Spanish
    ("negative", "No nos interesa, gracias.", "info@empresa.es"),
    ("negative", "No estoy interesado.", "info@empresa.es"),
    ("negative", "Por favor, dénos de baja de su lista.", "info@empresa.es"),
    ("negative", "No gracias.", "info@empresa.es"),
    ("positive", "Me interesa, envíame más información por favor.", "info@empresa.es"),
    ("positive", "Estamos interesados. ¿Podemos agendar una llamada?", "info@empresa.es"),
    ("positive", "Gracias por tu mensaje, nos interesa. ¿Cuánto cuesta?", "ana@empresa.es"),
    ("auto_reply", "Gracias por contactarnos. Te responderemos a la brevedad posible.", "info@empresa.es"),
    ("auto_reply", "Hemos recibido tu mensaje y nos pondremos en contacto contigo.", "info@empresa.es"),
    # Italian
    ("negative", "Non siamo interessati, grazie.", "info@azienda.it"),
    ("negative", "No grazie, non ci interessa.", "info@azienda.it"),
    ("positive", "Siamo interessati, ci mandi pure maggiori informazioni.", "info@azienda.it"),
    ("positive", "Mi interessa, possiamo sentirci domani?", "info@azienda.it"),
    ("auto_reply", "Questa è una mail automatica. La sua richiesta è stata ricevuta.", "info@azienda.it"),
    # Polish
    ("negative", "Dziekuje nie jestem zainteresowany", "kontakt@firma.pl"),
    ("negative", "Dziękujemy, nie jesteśmy zainteresowani.", "kontakt@firma.pl"),
    ("negative", "Proszę nie wysyłać więcej wiadomości.", "kontakt@firma.pl"),
    ("positive", "Jesteśmy zainteresowani, proszę o przesłanie oferty.", "kontakt@firma.pl"),
    ("auto_reply", "Otrzymaliśmy Twoją wiadomość. Odpowiemy najszybciej jak to możliwe.", "biuro@firma.pl"),
    # Danish / Swedish
    ("negative", "Tak, men vi er ikke interesserede.", "info@firma.dk"),
    ("negative", "Nej tack, vi är inte intresserade.", "info@foretag.se"),
    ("positive", "Vi er interesserede. Send gerne mere information.", "info@firma.dk"),
    ("auto_reply", "Vi har modtaget din henvendelse og vender tilbage hurtigst muligt.", "info@firma.dk"),
]


@pytest.mark.parametrize("expected,body,sender", I18N, ids=[_id(c[1]) for c in I18N])
def test_multilingual_replies(expected, body, sender):
    assert classify_sentiment(SUBJ, body, sender) == expected


# ── Precision guard ──────────────────────────────────────────────────────────

# Vacation notices say "call me" / "contact me" about urgent matters; none of
# that is interest. They must stay automated, never become Hot Leads.
@pytest.mark.parametrize("subject,body,sender", [
    ("Réponse automatique", "Je suis absent jusqu'au 12 novembre. En cas d'urgence, contactez-moi au 06 12 34 56 78.", "marie@societe.fr"),
    ("Fuera de oficina", "Estaré fuera de la oficina hasta el lunes. Si es urgente, llámame al 609 87 05 07 o escribe a Sara.", "eric@empresa.es"),
    ("Abwesenheitsnotiz", "Ich bin bis 10.11. nicht im Büro. In dringenden Fällen senden Sie mir bitte eine SMS.", "hans@firma.de"),
    ("Risposta automatica", "Sono fuori sede fino al 10 novembre. Per urgenze inviateci una mail a segreteria@azienda.it.", "info@azienda.it"),
    ("Odpowiedź automatyczna", "Jestem nieobecny do 10 listopada. W pilnych sprawach proszę o kontakt z biurem.", "jan@firma.pl"),
], ids=lambda v: _id(v))
def test_vacation_notices_with_call_to_action_stay_automated(subject, body, sender):
    assert classify_sentiment(subject, body, sender) == "auto_reply"


# A promotional footer is the sender's marketing copy, in any language.
@pytest.mark.parametrize("body,sender", [
    ("Vielen Dank für Ihre Nachricht. Wenn Sie interessiert sind, besuchen Sie unsere Website.", "info@firma.de"),
    ("Merci de votre message. Si vous êtes intéressé par nos services, consultez notre site.", "contact@societe.fr"),
    ("Gracias por escribirnos. Si estás interesado en nuestros cursos, visita la web.", "info@empresa.es"),
    ("Grazie. Se è interessato ai nostri servizi, visiti il sito.", "info@azienda.it"),
    ("Dear Interested Party: thank you. Our team will contact you.", "info@acme.com"),
    ("We would love to hear from you if anything changes.", H),
], ids=lambda v: _id(v))
def test_promotional_footers_are_not_leads(body, sender):
    assert classify_sentiment(SUBJ, body, sender) != "positive"


# A machine tag is not allowed to swallow a person: the auto-blocklist toggles
# act on it.
@pytest.mark.parametrize("body,sender", [
    ("That vendor is no longer available in our region, but I'm interested in your offer.", H),
    ("Our old system is no longer in use. Send me your pricing.", H),
    ("The page is no longer active, interested in a redesign?", H),
    ("Thanks for your message. It was received and I forwarded it to our CMO.", H),
    ("Gracias por tu mensaje, lo revisaremos y te responderemos pronto.", "ana@empresa.es"),
    ("Hallo, danke für Ihre Mail. Das Produkt wird nicht mehr verwendet, wir sind aber interessiert.", "hans@firma.de"),
    ("Gracias, nos interesa. Escríbeme por favor.", "ana@empresa.es"),
    ("Merci, ça m'intéresse. Appelez-moi demain.", "marc@societe.fr"),
    ("Danke, Interesse besteht. Können Sie mir ein Angebot schicken?", "hans@firma.de"),
], ids=lambda v: _id(v))
def test_humans_are_never_tagged_automated(body, sender):
    assert classify_sentiment(SUBJ, body, sender) != "auto_reply"
    assert _auto_reply_kind(SUBJ, body, sender) is None


@pytest.mark.parametrize("body,sender", [
    ("Thanks for reaching out — we're not interested, and please take us off your list.", H),
    ("Kein Interesse. Bitte keine weiteren E-Mails.", "hans@firma.de"),
    ("Pas intéressé, merci. Ne m'envoyez plus de mails.", "marc@societe.fr"),
], ids=lambda v: _id(v))
def test_refusal_beats_interest_words_in_the_same_reply(body, sender):
    assert classify_sentiment(SUBJ, body, sender) == "negative"


@pytest.mark.parametrize("body", [
    "Gracias, no estamos interesados.",
    "Nicht interessiert, danke.",
    "Nie jestem zainteresowany.",
    "Non siamo interessati.",
], ids=_id)
def test_not_interested_never_reads_as_interest(body):
    assert classify_sentiment(SUBJ, body, "x@y.com") == "negative"


# ── Scan behaviour ───────────────────────────────────────────────────────────

from tests.test_reply_filtering import _InboxIMAP, _LM, _raw  # noqa: E402


def _patch_state(tmp_path, monkeypatch, campaigns=None):
    import app.services.bounce_ingest as bi
    import app.services.imap_reply_tracker as T
    import app.services.reply_log as rl

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "reply_log.json")
    monkeypatch.setattr(rl, "PROCESSED_FILE", tmp_path / "reply_processed.json")
    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    monkeypatch.setattr(bi, "PROCESSED_FILE", tmp_path / "bounce_processed.json")
    monkeypatch.setattr(T.settings, "imap_host", "imap.example.com")
    monkeypatch.setattr(T.settings, "imap_user", "me@example.com")
    monkeypatch.setattr(T.settings, "imap_pass", "secret")

    async def _campaigns(listmonk, cutoff):
        return list(campaigns or []), True

    monkeypatch.setattr(T, "_fetch_campaigns_covering", _campaigns)
    return T, rl


@pytest.mark.asyncio
async def test_threaded_reply_from_unknown_address_is_not_kept(tmp_path, monkeypatch):
    """A threaded "Yes. Please share." from a non-subscriber address used to be
    kept unattributed. Replies without a campaign are never shown or counted,
    so the gate drops it like any other stranger's mail."""
    T, rl = _patch_state(tmp_path, monkeypatch)
    msgs = [
        _raw("admin@firm.example", "Re: Question About Firm?", "Yes. Please share.", "<t1@x>",
             "In-Reply-To: <sent-1@listmonk>\r\nReferences: <sent-1@listmonk>\r\n"),
        # Same sender, nothing answered: a stranger's mail, also dropped.
        _raw("stranger@other.example", "Hello there", "Interested? Yes!", "<t2@x>"),
    ]
    monkeypatch.setattr(T, "_connect_imap", lambda: _InboxIMAP(msgs))
    stats = await T._scan_and_track_replies_impl(_LM(), days_back=14)

    assert rl.load_log() == []
    assert stats["not_reply"] == 2
    assert {"<t1@x>", "<t2@x>"} <= rl.load_processed_message_ids()


@pytest.mark.asyncio
@pytest.mark.parametrize("body,sender", [
    # A ticket desk's acknowledgement, a polite decline from a desk, a stray "hi".
    ("Your incident was received. Reply above this line to add a comment. We would love to hear updates.",
     "help@desk.example"),
    ("Thank you for reaching out. We are not interested at this time.", "support@desk.example"),
    ("Hi", "someone@gmail.example"),
], ids=["automated", "decline", "neutral"])
async def test_threaded_mail_from_outside_the_campaign_is_not_kept(tmp_path, monkeypatch, body, sender):
    """Only mail that is part of a campaign is shown: a threaded answer from an
    address that is neither a subscriber nor a subject match is dropped, and
    recorded as handled so it is not re-examined every scan."""
    T, rl = _patch_state(tmp_path, monkeypatch)
    msgs = [_raw(sender, "Re: Question About Firm?", body, "<t3@x>",
                 "In-Reply-To: <sent-2@listmonk>\r\n")]
    monkeypatch.setattr(T, "_connect_imap", lambda: _InboxIMAP(msgs))
    stats = await T._scan_and_track_replies_impl(_LM(), days_back=14)

    assert rl.load_log() == []
    assert stats["not_reply"] == 1
    assert "<t3@x>" in rl.load_processed_message_ids()


@pytest.mark.asyncio
async def test_subscriber_reply_is_kept_and_attributed_via_their_lists(tmp_path, monkeypatch):
    """The campaign subject is a personalised template that never equals the
    reply subject, so a subscriber's own answer is attributed through their
    list memberships instead."""
    campaigns = [{"id": 5, "name": "Firm Outreach", "subject": "A personalised subject",
                  "created_at": "2026-01-01T00:00:00+00:00", "lists": [{"id": 1}], "body": ""}]
    T, rl = _patch_state(tmp_path, monkeypatch, campaigns)
    msgs = [_raw("prospect@example.com", "Re: Question About Firm?", "Not interested, thanks.", "<t4@x>",
                 "In-Reply-To: <sent-3@listmonk>\r\n")]
    monkeypatch.setattr(T, "_connect_imap", lambda: _InboxIMAP(msgs))
    await T._scan_and_track_replies_impl(_LM(), days_back=14)

    [record] = rl.load_log()
    assert record["from_email"] == "prospect@example.com"
    assert record["campaign_id"] == 5 and record["campaign_name"] == "Firm Outreach"
    assert record["campaign_checked"] is True


def _stored(rid, email, sentiment, **extra):
    return {"id": rid, "message_id": f"<{rid}@x>", "from_email": email, "subject": "Re: Hi",
            "date": "Wed, 14 Oct 2026 10:00:00 +0000", "full_body": "text", "body_preview": "text",
            "sentiment": sentiment, "status": "unread", "headers_checked": True,
            "campaign_id": None, "campaign_name": None,
            "mailbox": "me@example.com", **extra}


@pytest.mark.asyncio
async def test_prune_removes_stored_replies_outside_any_campaign(tmp_path, monkeypatch):
    """Replies stored by older versions: everything without a campaign goes
    -- strangers and unattributable subscriber answers alike, positive leads
    included -- because the feed never shows or counts them. Automated
    replies stay (they carry no campaign by design) and a human's triage is
    never destroyed."""
    T, rl = _patch_state(tmp_path, monkeypatch)
    rl.save_log([
        _stored("desk", "help@desk.example", "auto_reply"),                       # automated: kept
        _stored("decline", "support@desk.example", "negative"),                   # no campaign: gone
        _stored("hi", "someone@gmail.example", "neutral"),                        # no campaign: gone
        _stored("lead", "colleague@firm.example", "positive", status="lead"),     # auto-lead, gone
        _stored("camp", "colleague@firm.example", "negative", campaign_id=6, campaign_name="C"),
        _stored("touched", "x@desk.example", "neutral", status="archived"),       # human acted: kept
        _stored("manual", "y@desk.example", "neutral", status="lead",
                status_source="manual"),                                          # human acted: kept
    ])
    removed = await T.prune_non_campaign_replies()

    assert removed == 3
    kept = {r["id"] for r in rl.load_log()}
    assert kept == {"desk", "camp", "touched", "manual"}


@pytest.mark.asyncio
async def test_prune_keeps_replies_from_another_mailbox(tmp_path, monkeypatch):
    """Records stamped with another mailbox (a previous .env) are hidden by
    the feed but never deleted: an IMAP_USER change must not destroy triage
    work. Unstamped legacy records still go."""
    T, rl = _patch_state(tmp_path, monkeypatch)
    rl.save_log([
        _stored("foreign-camp", "old@other.example", "negative",
                campaign_id=6, campaign_name="C", mailbox="old@other.example"),
        _stored("foreign-stray", "old@other.example", "neutral",
                mailbox="old@other.example"),
        # Unstamped = written before the mailbox field existed.
        _stored("legacy", "old@other.example", "auto_reply", mailbox=None),
        _stored("mine", "prospect@example.com", "auto_reply"),
    ])
    removed = await T.prune_non_campaign_replies()

    assert removed == 1
    assert [r["id"] for r in rl.load_log()] == ["foreign-camp", "foreign-stray", "mine"]


@pytest.mark.asyncio
async def test_prune_is_idempotent(tmp_path, monkeypatch):
    T, rl = _patch_state(tmp_path, monkeypatch)
    rl.save_log([
        _stored("decline", "support@desk.example", "negative"),
        _stored("desk", "help@desk.example", "auto_reply"),
    ])
    assert await T.prune_non_campaign_replies() == 1
    assert await T.prune_non_campaign_replies() == 0
    assert [r["id"] for r in rl.load_log()] == ["desk"]


@pytest.mark.asyncio
async def test_prune_needs_no_listmonk_lookups(tmp_path, monkeypatch):
    """Attribution on the record is the only evidence: the prune is pure
    state-file work, so a ListMonk outage cannot block or skew it."""
    T, rl = _patch_state(tmp_path, monkeypatch)
    rl.save_log([_stored("decline", "support@desk.example", "negative")])
    assert await T.prune_non_campaign_replies() == 1
    assert rl.load_log() == []


def test_header_backfill_targets_only_unchecked_records():
    records = [
        {"message_id": "<a@x>"},                                    # legacy: never header-checked
        {"message_id": "<b@x>", "headers_checked": True},           # already judged
        {"message_id": "<c@x>", "auto_header": "auto_replied"},     # verdict stored
        {"message_id": ""},                                         # no key to fetch by
    ]
    assert _header_backfill_ids(records) == {"<a@x>"}


def test_collect_header_backfills_reads_headers_and_records_silence():
    import email
    import email.policy

    def parse(raw):
        return email.message_from_bytes(raw, policy=email.policy.default)

    responder = parse(_raw("a@x.com", "Re: Hi", "Thanks.", "<a@x>", "Auto-Submitted: auto-replied\r\n"))
    human = parse(_raw("b@x.com", "Re: Hi", "Thanks.", "<b@x>"))
    other = parse(_raw("c@x.com", "Re: Hi", "Thanks.", "<c@x>", "Auto-Submitted: auto-replied\r\n"))
    found = collect_header_backfills([responder, human, other], {"<a@x>", "<b@x>"})
    # <b@x> maps to None: checked, says nothing — so it is not fetched again.
    assert found == {"<a@x>": "auto_replied", "<b@x>": None}


@pytest.mark.asyncio
async def test_scan_judges_legacy_records_by_their_automation_headers(tmp_path, monkeypatch):
    """A responder with no tell-tale wording stayed a neutral reply (and a fake
    Hot Lead when positive) forever: its Auto-Submitted header was never read."""
    T, rl = _patch_state(tmp_path, monkeypatch)
    rl.save_log([
        {"id": "rep_old", "message_id": "<legacy@x>", "from_email": "inscripcion@models.example",
         "subject": "Inscripcion", "date": "Wed, 14 Oct 2026 10:00:00 +0000",
         "full_body": "Gracias por comunicarte con nosotros. Estaremos completando tu ficha.",
         "sentiment": "neutral", "status": "unread", "status_source": "auto",
         "campaign_id": 6, "campaign_name": "Mixed Leads 12K",
         "mailbox": "me@example.com"},
    ])
    await rl.mark_processed_message_ids(["<legacy@x>"])
    msgs = [_raw("inscripcion@models.example", "Inscripcion",
                 "Gracias por comunicarte con nosotros. Estaremos completando tu ficha.",
                 "<legacy@x>", "Auto-Submitted: auto-replied\r\n")]
    monkeypatch.setattr(T, "_connect_imap", lambda: _InboxIMAP(msgs))

    stats = await T._scan_and_track_replies_impl(_LM(), days_back=14)

    [record] = rl.load_log()
    assert record["auto_header"] == "auto_replied" and record["headers_checked"] is True
    assert record["sentiment"] == "auto_reply"
    assert record["campaign_id"] is None and record["campaign_name"] is None
    assert stats["new_replies"] == 0  # a backfill never creates a second record


@pytest.mark.asyncio
async def test_backfill_demotes_an_auto_promoted_lead(tmp_path, monkeypatch):
    T, rl = _patch_state(tmp_path, monkeypatch)
    rl.save_log([
        {"id": "rep_lead", "message_id": "<fake-lead@x>", "from_email": "desk@vendor.example",
         "subject": "Re: Question", "date": "Wed, 14 Oct 2026 10:00:00 +0000",
         "full_body": "Sure, we would love to help. Interested parties may reply.",
         "sentiment": "positive", "status": "lead", "status_source": "auto",
         "campaign_id": 7, "campaign_name": "C", "mailbox": "me@example.com"},
    ])
    await rl.mark_processed_message_ids(["<fake-lead@x>"])
    msgs = [_raw("desk@vendor.example", "Re: Question",
                 "Sure, we would love to help. Interested parties may reply.",
                 "<fake-lead@x>", "Auto-Submitted: auto-generated\r\n")]
    monkeypatch.setattr(T, "_connect_imap", lambda: _InboxIMAP(msgs))
    await T._scan_and_track_replies_impl(_LM(), days_back=14)

    [record] = rl.load_log()
    assert record["sentiment"] == "auto_reply"
    assert record["status"] == "unread"  # the automatic promotion is undone
    # An automated reply never carries a campaign, even one it was stored with.
    assert record["campaign_id"] is None


@pytest.mark.asyncio
async def test_backfill_leaves_a_human_chosen_status_alone(tmp_path, monkeypatch):
    T, rl = _patch_state(tmp_path, monkeypatch)
    rl.save_log([
        {"id": "rep_h", "message_id": "<human@x>", "from_email": "desk@vendor.example",
         "subject": "Re: Question", "date": "Wed, 14 Oct 2026 10:00:00 +0000",
         "full_body": "Thanks, noted.",
         "sentiment": "neutral", "status": "lead", "status_source": "manual",
         "mailbox": "me@example.com"},
    ])
    await rl.mark_processed_message_ids(["<human@x>"])
    msgs = [_raw("desk@vendor.example", "Re: Question", "Thanks, noted.", "<human@x>",
                 "Auto-Submitted: auto-replied\r\n")]
    monkeypatch.setattr(T, "_connect_imap", lambda: _InboxIMAP(msgs))
    await T._scan_and_track_replies_impl(_LM(), days_back=14)

    [record] = rl.load_log()
    assert record["sentiment"] == "auto_reply"
    assert record["status"] == "lead"  # the user's own choice is never overwritten


@pytest.mark.asyncio
async def test_reclassify_clears_stale_campaign_on_unchanged_auto_reply(tmp_path, monkeypatch):
    """Records tagged before attribution was skipped for automated mail kept a
    campaign, inflating the per-campaign reply chart."""
    import app.services.imap_reply_tracker as T
    import app.services.reply_log as rl

    monkeypatch.setattr(rl, "LOG_FILE", tmp_path / "reply_log.json")
    monkeypatch.setattr(rl, "SETTINGS_FILE", tmp_path / "reply_settings.json")
    rl.save_log([
        {"id": "rep_1", "message_id": "<1@x>", "from_email": "support@forever.example",
         "subject": "Request received: Question About Forever - Ticket number 514499",
         "full_body": "Your request (514499) has been received and is being reviewed by the Client Care Team.",
         "sentiment": "auto_reply", "status": "unread", "campaign_id": 6, "campaign_name": "Mixed Leads 12K"},
        {"id": "rep_2", "message_id": "<2@x>", "from_email": "jane@acme.com", "subject": "Re: Hi",
         "full_body": "Yes. Please share.", "sentiment": "positive", "status": "lead",
         "status_source": "auto", "campaign_id": 6, "campaign_name": "Mixed Leads 12K"},
    ])
    assert await T.reclassify_stored_sentiments() == 1
    r1, r2 = rl.load_log()
    assert r1["campaign_id"] is None and r1["campaign_name"] is None
    assert r2["campaign_id"] == 6  # a real reply keeps its campaign


# ── Unsubscribe scanner: plural opt-outs ─────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Please remove us from your list.",
    "unsubscribe us",
    "Kindly exclude us from future mailings",
    "Please remove me from your list",
])
def test_unsubscribe_scanner_acts_on_company_opt_outs(text):
    assert KEYWORD_PATTERN.search(text)


def test_unsubscribe_scanner_ignores_unrelated_text():
    assert not KEYWORD_PATTERN.search("Please remove the old logo from the deck and send a quote.")


def test_live_settings_shape_is_not_required(tmp_path):
    """Records written by the scan carry JSON-safe values only."""
    json.dumps({"headers_checked": True, "auto_header": None})


# ── Header backfill beyond the scan window ───────────────────────────────────

class _ArchiveIMAP:
    """The SINCE search sees ``window``; ``SEARCH HEADER Message-ID`` also
    reaches ``archive`` — mail older than the scan window, like the real inbox."""

    def __init__(self, window, archive=(), fail_lookup=False):
        self.window = list(window)
        self.everything = self.window + list(archive)
        self.fail_lookup = fail_lookup
        self.header_searches = []
        self.fetch_specs = []

    def select(self, *a, **k):
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        if criteria and criteria[0] == "HEADER":
            if self.fail_lookup:
                raise imaplib.IMAP4.error("server hiccup")
            wanted = criteria[2].strip('"')
            self.header_searches.append(wanted)
            # Like a real server: a substring match inside the header's value.
            nums = [str(i + 1).encode() for i, raw in enumerate(self.everything)
                    if any(line.lower().startswith(b"message-id:") and wanted.encode() in line
                           for line in raw.split(b"\r\n"))]
            return "OK", [b" ".join(nums)]
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.window)))]

    def fetch(self, num, spec):
        self.fetch_specs.append(spec)
        return "OK", [(num, self.everything[int(num) - 1])]

    def close(self):
        pass

    def logout(self):
        pass


def test_lookup_reads_headers_of_mail_older_than_the_window():
    responder = _raw("a@x.com", "Out of Office", "Back Monday.", "<old-auto@x>", "Auto-Submitted: auto-replied\r\n")
    human = _raw("b@x.com", "Re: Hi", "Sounds good.", "<old-human@x>")
    mail = _ArchiveIMAP(window=[], archive=[responder, human])

    found = lookup_header_verdicts(mail, {"<old-auto@x>", "<old-human@x>", "<gone@x>"})

    # None = the headers say nothing, or the message left the INBOX; either way final.
    assert found == {"<old-auto@x>": "auto_replied", "<old-human@x>": None, "<gone@x>": None}
    # Only the headers are read, and PEEK keeps the human's unread state.
    assert set(mail.fetch_specs) == {"(BODY.PEEK[HEADER])"}


def test_lookup_never_puts_unsafe_ids_into_a_search():
    """Message-IDs come from strangers: a quote or CRLF would inject IMAP syntax."""
    mail = _ArchiveIMAP(window=[])
    hostile = {'<a"b@x>', "<a\r\nA1 DELETE INBOX@x>", "<a\\b@x>", "no-brackets", "<" + "a" * 300 + "@x>"}

    found = lookup_header_verdicts(mail, hostile)

    assert mail.header_searches == []
    assert found == {mid: None for mid in hostile}  # judged final, not retried forever


def test_lookup_error_is_not_final():
    mail = _ArchiveIMAP(window=[], fail_lookup=True)
    assert lookup_header_verdicts(mail, {"<old@x>"}) == {}  # left unchecked for the next scan


def test_lookup_is_bounded_per_scan():
    mail = _ArchiveIMAP(window=[])
    ids = {f"<m{i:03d}@x>" for i in range(HEADER_LOOKUP_LIMIT + 25)}
    assert len(lookup_header_verdicts(mail, ids)) == HEADER_LOOKUP_LIMIT


def test_lookup_ignores_a_longer_id_that_merely_contains_the_wanted_one():
    """SEARCH HEADER is a substring match; ``<x@y>`` must not adopt ``<x@y>``'s
    neighbour carrying an Auto-Submitted header."""
    other = _raw("a@x.com", "Re: Hi", "Thanks.", "<xx@y>", "Auto-Submitted: auto-replied\r\n")
    mail = _ArchiveIMAP(window=[], archive=[other])
    mail.everything[0] = other.replace(b"Message-ID: <xx@y>", b"Message-ID: <wrapper<x@y>>")
    assert lookup_header_verdicts(mail, {"<x@y>"}) == {"<x@y>": None}


@pytest.mark.asyncio
async def test_scan_backfills_a_record_older_than_the_scan_window(tmp_path, monkeypatch):
    """Live case: 14 replies were 2-4 weeks old, outside the 14-day window, so the
    header backfill never reached them although the messages were still there."""
    T, rl = _patch_state(tmp_path, monkeypatch)
    rl.save_log([
        {"id": "rep_old", "message_id": "<old-auto@x>", "from_email": "desk@vendor.example",
         "subject": "Request received", "date": "Mon, 05 Oct 2026 10:00:00 +0000",
         "full_body": "We are looking into it.", "sentiment": "neutral", "status": "unread",
         "status_source": "auto", "campaign_id": 6, "campaign_name": "Mixed Leads 12K",
         "mailbox": "me@example.com"},
        {"id": "rep_gone", "message_id": "<gone@x>", "from_email": "jane@acme.com",
         "subject": "Re: Hi", "date": "Mon, 05 Oct 2026 11:00:00 +0000",
         "full_body": "Who is this?", "sentiment": "neutral", "status": "unread", "status_source": "auto",
         "campaign_id": 6, "campaign_name": "Mixed Leads 12K", "mailbox": "me@example.com"},
    ])
    await rl.mark_processed_message_ids(["<old-auto@x>", "<gone@x>"])
    archived = _raw("desk@vendor.example", "Request received", "We are looking into it.",
                    "<old-auto@x>", "Auto-Submitted: auto-replied\r\n")
    mail = _ArchiveIMAP(window=[], archive=[archived])
    monkeypatch.setattr(T, "_connect_imap", lambda: mail)

    await T._scan_and_track_replies_impl(_LM(), days_back=14)

    auto, gone = rl.load_log()
    assert auto["auto_header"] == "auto_replied" and auto["headers_checked"] is True
    assert auto["sentiment"] == "auto_reply" and auto["campaign_id"] is None
    # A message that left the INBOX is closed out, not searched for on every scan.
    assert gone["headers_checked"] is True and "auto_header" not in gone
    assert gone["sentiment"] == "neutral"

    mail.header_searches.clear()
    await T._scan_and_track_replies_impl(_LM(), days_back=14)
    assert mail.header_searches == []  # nothing left to look up: no extra IMAP work


@pytest.mark.asyncio
async def test_scan_retries_the_lookup_after_an_imap_error(tmp_path, monkeypatch):
    T, rl = _patch_state(tmp_path, monkeypatch)
    rl.save_log([
        {"id": "rep_old", "message_id": "<old-auto@x>", "from_email": "desk@vendor.example",
         "subject": "Request received", "date": "Mon, 05 Oct 2026 10:00:00 +0000",
         "full_body": "We are looking into it.", "sentiment": "neutral", "status": "unread",
         "status_source": "auto", "campaign_id": 6, "campaign_name": "Mixed Leads 12K",
         "mailbox": "me@example.com"},
    ])
    await rl.mark_processed_message_ids(["<old-auto@x>"])
    archived = _raw("desk@vendor.example", "Request received", "We are looking into it.",
                    "<old-auto@x>", "Auto-Submitted: auto-replied\r\n")
    broken = _ArchiveIMAP(window=[], archive=[archived], fail_lookup=True)
    monkeypatch.setattr(T, "_connect_imap", lambda: broken)
    await T._scan_and_track_replies_impl(_LM(), days_back=14)
    assert "headers_checked" not in rl.load_log()[0]  # a hiccup must not close the record out

    healthy = _ArchiveIMAP(window=[], archive=[archived])
    monkeypatch.setattr(T, "_connect_imap", lambda: healthy)
    await T._scan_and_track_replies_impl(_LM(), days_back=14)
    assert rl.load_log()[0]["sentiment"] == "auto_reply"


# ── Unsubscribe scanner: opt-out vocabulary ──────────────────────────────────

OPT_OUTS = [
    # English
    "Please unsubscribe.", "Kindly unsubscribe us from this.", "I would like to unsubscribe",
    "We'd like to unsubscribe", "Take us off your mailing list", "take me off your list please",
    "Please remove my email address from your list", "Delete our data", "Please stop emailing me",
    "Stop sending us these", "Do not contact us again", "Don't email me", "Never contact me again",
    "No more emails please", "No further contact", "Opt me out", "We want to opt out of your emails",
    "Unsubscribe", "STOP", "Remove", "Stop.\n\nSam\nCEO",
    # German
    "Bitte entfernen Sie uns aus Ihrem Verteiler.", "Löschen Sie mich bitte aus Ihrer Liste",
    "Bitte uns aus dem Verteiler entfernen", "Nehmen Sie uns bitte aus Ihrem Verteiler",
    "Senden Sie uns bitte keine Werbung mehr", "Kontaktieren Sie uns bitte nicht mehr",
    "Bitte uns nicht mehr kontaktieren", "Bitte keine Mails mehr",
    # French
    "Désinscrivez-moi de votre liste", "Retirez-nous de votre liste svp", "Merci de me désinscrire",
    "Merci de nous retirer de votre liste de diffusion", "Ne nous contactez plus",
    "Veuillez ne plus nous envoyer de mails", "Supprimez-moi de votre base",
    # Spanish
    "Por favor, darme de baja", "Quiero darnos de baja de su lista", "Elimínenme de su lista",
    "No me escriban más", "Dejen de escribirnos", "Bórrenos de su base de datos", "Quítenme de la lista",
    # Italian
    "Cancellatemi dalla vostra lista", "Rimuovetemi per favore", "Toglieteci dalla mailing list",
    "Non contattateci più", "Non ci inviate altre email",
    # Portuguese
    "Remova-me da lista", "Retirem-nos da vossa lista", "Não nos envie mais emails",
    # Dutch
    "Verwijder ons van uw lijst", "Schrijf ons alstublieft uit", "Meld ons af", "Geen mails meer",
    # Polish
    "Proszę usunąć nas z listy", "Usuńcie mnie z waszej bazy", "Wypiszcie nas",
    "Proszę nas usunąć z listy mailingowej", "Proszę nie wysyłać więcej maili", "Nie piszcie do nas",
    # Danish / Norwegian / Swedish
    "Fjern os fra jeres liste", "Slet mig fra listen", "Meld mig af", "Ikke flere mails tak",
    "Ta bort oss från er lista", "Ta oss av listen", "Skicka inte fler mejl",
]

# Sentences a person could write that share words with an opt-out. Acting on
# one unenrols (and may blocklist) a prospect who asked for nothing of the kind.
NOT_OPT_OUTS = [
    "Please remove the old logo from the deck and send a quote.",
    "Don't stop emailing me, this is useful!", "Please don't stop sending the case studies",
    "We never stop sending updates to clients", "I will unsubscribe from the other newsletter later",
    "Remove my email from this thread please, wrong cc", "Take me off the CC line",
    "Can you remove the second paragraph and resend?", "We have no further questions, send the quote",
    "Send me more details, no more emails needed once we talk", "Stop by our office",
    "To unsubscribe from our newsletter click here", "Unsubscribe from this newsletter: link",
    "Thanks, will review", "I need to remove a few items from the proposal",
    "Please delete the attachment I sent earlier", "Delete my previous message, wrong file",
    "Not interested, thanks", "No thanks", "We are all set for now",
    "Cancellatemi l'appuntamento di lunedì", "Mandateci il preventivo, non rimuovete il logo",
    "Wir melden uns, bitte senden Sie uns ein Angebot", "Löschen Sie bitte den Anhang",
    "Bitte senden Sie uns weitere Informationen", "Entfernen Sie bitte das Logo", "Keine Sorge, wir melden uns",
    "Merci de nous envoyer un devis", "Retirez le logo svp", "Pouvez-vous me renvoyer le devis",
    "Je vais me retirer quelques jours pour les vacances", "Supprimez le dernier paragraphe",
    "Quiero darle de baja al servicio de mi madre", "Por favor envíenme el presupuesto", "Llámame mañana",
    "Quitar el logotipo por favor", "No me llamen hoy, estoy en una reunión", "Estoy retirado de la oficina",
    "Quitamos el logo", "No nos llamen antes de las 9",
    "Mandateci il preventivo", "Contattateci domani", "Toglietemi un dubbio",
    "Envie-me o orçamento", "Retire o logotipo", "Não envie anexos grandes",
    "Stuur ons de offerte", "Verwijder de bijlage", "Meld je aan voor de nieuwsbrief",
    "Proszę przesłać ofertę", "Nie jestem zainteresowany", "Dziękujemy, odezwiemy się", "Usuń załącznik",
    "Send os et tilbud", "Meld mig til nyhedsbrevet", "Fjern logoet", "Slet vedhæftningen",
    "Skicka offert", "Ta bort bilagan",
    "I am out of the office until Monday with limited access to email.",
    "Ich bin bis zum 26.10. nicht im Büro und habe keinen Zugriff auf E-Mails.",
    "Estaré fuera de la oficina hasta el lunes.",
]


@pytest.mark.parametrize("text", OPT_OUTS, ids=_id)
def test_unsubscribe_scanner_recognises_opt_out(text):
    assert KEYWORD_PATTERN.search(text), text


@pytest.mark.parametrize("text", NOT_OPT_OUTS, ids=_id)
def test_unsubscribe_scanner_ignores_lookalikes(text):
    match = KEYWORD_PATTERN.search(text)
    assert not match, f"{text!r} matched {match.group(0)!r}"


async def _unsub_scan(tmp_path, monkeypatch, body, extra_headers=None):
    """Run the real unsubscribe scan over one message; returns (result, list-modify calls)."""
    import app.services.unsubscribe_log as shared_log
    from app.services import imap_unsubscribe as svc
    from app.services.listmonk_client import ListMonkClient
    from tests.test_reply_filtering import _UnsubIMAP

    for name in ("LOG_FILE", "SETTINGS_FILE", "PROCESSED_FILE", "EXAMINED_FILE"):
        monkeypatch.setattr(shared_log, name, tmp_path / f"{name}.json")

    msg = email.message.EmailMessage()
    msg["From"] = "user@example.com"
    msg["Subject"] = "Re: Growth idea"
    msg["Message-ID"] = "<opt@example.com>"
    msg["Date"] = "Mon, 01 Sep 2025 12:00:00 +0000"
    for key, value in (extra_headers or {}).items():
        msg[key] = value
    msg.set_content(body)
    monkeypatch.setattr(svc, "connect_imap", lambda: _UnsubIMAP(msg.as_bytes()))

    modify_calls = []

    async def mock_request(method, path, **kwargs):
        if method == "GET" and path == "/api/campaigns":
            return {"data": {"results": [{"id": 10, "name": "September", "created_at": "2025-09-01",
                                          "lists": [{"id": 1}], "body": "<p>Hello</p>"}], "total": 1}}
        if method == "GET" and "/api/subscribers" in path:
            return {"data": {"results": [{"id": 5, "email": "user@example.com",
                                          "name": "User", "lists": [{"id": 1}]}], "total": 1}}
        if method == "PUT" and "/api/subscribers/lists" in path:
            modify_calls.append(path)
        return {"data": {}}

    client = ListMonkClient.__new__(ListMonkClient)
    client._client = None
    client._request = mock_request
    return await svc.scan_and_unsubscribe(client), modify_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "Please stop emailing me.",
    "Proszę usunąć nas z listy.",
    "Bitte entfernen Sie uns aus Ihrem Verteiler.",
    "Unsubscribe",
])
async def test_unsubscribe_scan_acts_on_wider_opt_outs(tmp_path, monkeypatch, body):
    result, modify_calls = await _unsub_scan(tmp_path, monkeypatch, body)
    assert result["processed"] == 1 and len(modify_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    "Nie jestem zainteresowany",                          # a refusal is not a request to stop mail
    "Thank you very much, we are not exploring this type of collaboration.",
    "Thanks, will review.",
])
async def test_unsubscribe_scan_leaves_plain_refusals_alone(tmp_path, monkeypatch, body):
    result, modify_calls = await _unsub_scan(tmp_path, monkeypatch, body)
    assert result["processed"] == 0 and modify_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {"Auto-Submitted": "auto-replied"},
    {"X-Autoreply": "yes"},
    {"Precedence": "auto_reply"},
])
async def test_unsubscribe_scan_never_acts_on_automated_mail(tmp_path, monkeypatch, headers):
    """A responder's footer ("to unsubscribe, reply STOP") is boilerplate, not a
    request; acting on it would unenrol a real prospect who is only away."""
    body = "I am out of the office until Monday.\n\nPlease stop emailing me at this address, remove us from your list."
    result, modify_calls = await _unsub_scan(tmp_path, monkeypatch, body, headers)
    assert result["processed"] == 0 and modify_calls == []
