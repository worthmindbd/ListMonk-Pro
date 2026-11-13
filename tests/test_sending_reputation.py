"""Sending reputation checks (Deliverability → Sending reputation).

Every case here is one seen on the live servers on 2026-11-12: an IP whose
reverse DNS named another domain (Yahoo 550 5.7.25), an IP on Barracuda,
Gmail "likely unsolicited" rejections filed as plain soft bounces, and
Spamhaus refusing to answer a public resolver. No network: ``_query`` is
replaced by a DNS table.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import dns.exception
import pytest

from app.main import app
from app.services import sending_reputation as sr
from app.services.bounce_ingest import classify_bounce
from tests.conftest import CSRFTestClient

TIMEOUT = object()


class FakeDNS:
    def __init__(self, table=None):
        self.table = dict(table or {})
        self.calls = []

    async def __call__(self, name, rdtype):
        self.calls.append((name, rdtype))
        value = self.table.get((name.lower(), rdtype), [])
        if value is TIMEOUT:
            raise dns.exception.Timeout()
        return list(value)


def working_lists(table, except_zones=()):
    """Every DNSBL answers its RFC 5782 test entry, except ``except_zones``."""
    for _, zone, _ in sr.DNSBLS:
        if zone not in except_zones:
            table[(f"2.0.0.127.{zone}", "A")] = ["127.0.0.2"]
    return table


@pytest.fixture
def fake_dns(monkeypatch):
    fake = FakeDNS()
    monkeypatch.setattr(sr, "_query", fake)
    return fake


SETTINGS = {
    "app.from_email": "Sender <sender@example.org>",
    "smtp": [{"enabled": True, "host": "mail.example.org"}, {"enabled": False, "host": "old.example.com"}],
}


def healthy_table():
    t = {
        ("mail.example.org", "A"): ["93.184.216.34"],
        ("34.216.184.93.in-addr.arpa.", "PTR"): ["mail.example.org"],
        ("example.org", "TXT"): ["v=spf1 a mx ip4:93.184.216.34 -all"],
        ("example.org", "A"): ["93.184.216.34"],
        ("example.org", "MX"): ["mail.example.org"],
        ("mail._domainkey.example.org", "TXT"): ["v=DKIM1; k=rsa; p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA"],
        ("_dmarc.example.org", "TXT"): ["v=DMARC1; p=quarantine; rua=mailto:d@example.org"],
    }
    return working_lists(t)


def lm_with_bounces(bounces=()):
    lm = MagicMock()
    lm.get_bounces = AsyncMock(return_value={"data": {"results": list(bounces)}})
    lm.get_settings = AsyncMock(return_value={"data": SETTINGS})
    return lm


def by_id(report):
    return {c["id"]: c for c in sr.build_checks(report)}


# ── Sending IPs ─────────────────────────────────────────────────────────────

async def test_ips_come_from_enabled_smtp_hosts(fake_dns):
    fake_dns.table.update(healthy_table())
    found = await sr.find_sending_ips(SETTINGS)
    assert found["ips"] == ["93.184.216.34"] and found["hosts"] == ["mail.example.org"]
    assert ("old.example.com", "A") not in fake_dns.calls


async def test_private_relay_asks_for_sending_ips(fake_dns):
    fake_dns.table[("relay", "A")] = ["172.18.0.3"]
    found = await sr.find_sending_ips({"smtp": [{"enabled": True, "host": "relay"}]})
    assert found["ips"] == [] and "SENDING_IPS" in found["note"]


async def test_env_override_and_known_provider(fake_dns, monkeypatch):
    monkeypatch.setattr(sr.settings, "sending_ips", ("203.0.113.9", "10.0.0.1"))
    assert (await sr.find_sending_ips(SETTINGS))["ips"] == []   # documentation/private ranges are not global
    monkeypatch.setattr(sr.settings, "sending_ips", ("8.8.4.4",))
    assert (await sr.find_sending_ips(SETTINGS))["source"] == "env"
    monkeypatch.setattr(sr.settings, "sending_ips", ())
    fake_dns.table[("email-smtp.eu-west-1.amazonaws.com", "A")] = ["52.1.2.3"]
    found = await sr.find_sending_ips({"smtp": [{"enabled": True, "host": "email-smtp.eu-west-1.amazonaws.com"}]})
    assert found["provider"] == "Amazon SES"


# ── Reverse DNS ─────────────────────────────────────────────────────────────

async def test_reverse_dns_pointing_at_another_domain_fails(fake_dns):
    t = healthy_table()
    # The live bug: the IP named after another domain's host.
    t[("34.216.184.93.in-addr.arpa.", "PTR")] = ["host.other-domain.example"]
    t[("host.other-domain.example", "A")] = ["93.184.216.35"]
    fake_dns.table.update(t)
    rep = await sr.get_report(lm_with_bounces(), SETTINGS)
    checks = by_id(rep)
    assert checks["reverse_dns"]["status"] == "fail"
    assert "points elsewhere" in checks["reverse_dns"]["current_value"]
    assert any("does not point back" in a["text"] for a in sr.alerts(rep))


async def test_healthy_setup_passes_everything(fake_dns):
    fake_dns.table.update(healthy_table())
    fake_dns.table[("mail.example.org", "A")] = ["93.184.216.34"]
    rep = await sr.get_report(lm_with_bounces(), SETTINGS)
    statuses = {k: v["status"] for k, v in by_id(rep).items()}
    assert statuses == {
        "reverse_dns": "pass", "ptr_alignment": "pass", "ip_blocklists": "pass", "spf": "pass",
        "dkim": "pass", "dmarc": "pass", "sender_rejections": "pass",
    }
    assert sr.alerts(rep) == []


async def test_missing_ptr_and_misaligned_name(fake_dns):
    t = healthy_table()
    del t[("34.216.184.93.in-addr.arpa.", "PTR")]
    fake_dns.table.update(t)
    rep = await sr.get_report(lm_with_bounces(), SETTINGS)
    assert by_id(rep)["reverse_dns"]["status"] == "fail"
    sr.invalidate()
    t[("34.216.184.93.in-addr.arpa.", "PTR")] = ["vps123.hosting.example"]
    t[("vps123.hosting.example", "A")] = ["93.184.216.34"]
    fake_dns.table.update(t)
    rep = await sr.get_report(lm_with_bounces(), SETTINGS, refresh=True)
    checks = by_id(rep)
    assert checks["reverse_dns"]["status"] == "pass"
    assert checks["ptr_alignment"]["status"] == "warn"


# ── Blocklists ──────────────────────────────────────────────────────────────

async def test_listed_ip_fails_with_removal_link(fake_dns):
    t = healthy_table()
    t[("34.216.184.93.b.barracudacentral.org", "A")] = ["127.0.0.2"]
    fake_dns.table.update(t)
    rep = await sr.get_report(lm_with_bounces(), SETTINGS)
    check = by_id(rep)["ip_blocklists"]
    assert check["status"] == "fail"
    assert any("barracudacentral.org/rbl/removal-request" in link["url"] for link in check["links"])
    assert any("Barracuda" in a["text"] and a["tone"] == "danger" for a in sr.alerts(rep))


async def test_list_refusing_queries_is_unknown_never_clean(fake_dns):
    # Spamhaus answers public resolvers with 127.255.255.254 (also for its
    # test entry): that is a refusal, not "not listed".
    t = working_lists(healthy_table(), except_zones=("zen.spamhaus.org",))
    t[("2.0.0.127.zen.spamhaus.org", "A")] = ["127.255.255.254"]
    t[("34.216.184.93.zen.spamhaus.org", "A")] = ["127.255.255.254"]
    fake_dns.table.update(t)
    rep = await sr.get_report(lm_with_bounces(), SETTINGS)
    spamhaus = [r for r in rep["blocklists"]["results"] if r["list"] == "Spamhaus ZEN"]
    assert spamhaus[0]["status"] == "unknown"
    check = by_id(rep)["ip_blocklists"]
    assert check["status"] == "pass"
    assert {"label": "Spamhaus ZEN: not checkable here", "tone": "warn"} in check["chips"]
    assert any("check.spamhaus.org" in link["url"] for link in check["links"])


async def test_no_working_list_is_info(fake_dns):
    t = healthy_table()
    for _, zone, _ in sr.DNSBLS:
        t.pop((f"2.0.0.127.{zone}", "A"), None)
    fake_dns.table.update(t)
    rep = await sr.get_report(lm_with_bounces(), SETTINGS)
    assert by_id(rep)["ip_blocklists"]["status"] == "info"


# ── SPF ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("record, extra, expected", [
    ("v=spf1 ip4:93.184.216.0/24 -all", {}, "pass"),
    ("v=spf1 ip4:1.2.3.4 -all", {}, "fail"),
    ("v=spf1 ip4:1.2.3.4 ~all", {}, "softfail"),
    ("v=spf1 ip4:1.2.3.4", {}, "neutral"),
    ("v=spf1 a/24 -all", {("d.test", "A"): ["93.184.216.1"]}, "pass"),
    ("v=spf1 mx:other.test -all", {("other.test", "MX"): ["mx.other.test"],
                                   ("mx.other.test", "A"): ["93.184.216.34"]}, "pass"),
    ("v=spf1 include:esp.test -all", {("esp.test", "TXT"): ["v=spf1 ip4:93.184.216.34 -all"]}, "pass"),
    ("v=spf1 include:missing.test -all", {}, "permerror"),
    ("v=spf1 redirect=esp.test", {("esp.test", "TXT"): ["v=spf1 ip4:93.184.216.34 -all"]}, "pass"),
    ("v=spf1 exists:%{i}.x.test -all", {}, "fail"),     # macros skipped, falls to -all
])
async def test_spf_evaluation(fake_dns, record, extra, expected):
    fake_dns.table[("d.test", "TXT")] = [record]
    fake_dns.table.update(extra)
    result = await sr.check_spf("d.test", ["93.184.216.34"])
    assert result["results"][0]["result"] == expected


async def test_spf_lookup_limit_and_duplicate_records(fake_dns):
    chain = " ".join(f"include:i{n}.test" for n in range(11))
    fake_dns.table[("d.test", "TXT")] = [f"v=spf1 {chain} -all"]
    for n in range(11):
        fake_dns.table[(f"i{n}.test", "TXT")] = ["v=spf1 -all"]
    assert (await sr.check_spf("d.test", ["93.184.216.34"]))["results"][0]["result"] == "permerror"
    fake_dns.table[("two.test", "TXT")] = ["v=spf1 -all", "v=spf1 +all"]
    assert (await sr.check_spf("two.test", ["93.184.216.34"]))["status"] == "permerror"


async def test_spf_missing_record_fails_the_check(fake_dns):
    t = healthy_table()
    t[("example.org", "TXT")] = ["google-site-verification=abc"]
    fake_dns.table.update(t)
    rep = await sr.get_report(lm_with_bounces(), SETTINGS)
    assert by_id(rep)["spf"]["status"] == "fail"


# ── DKIM / DMARC ────────────────────────────────────────────────────────────

async def test_dkim_and_dmarc(fake_dns):
    fake_dns.table[("_dmarc.example.com", "TXT")] = ["v=DMARC1; p=none"]
    dmarc = await sr.check_dmarc("news.example.com")   # inherits the org domain policy
    assert dmarc["found"] and dmarc["policy"] == "none"
    assert (await sr.check_dmarc("nodmarc.test"))["found"] is False
    assert (await sr.check_dkim("nodmarc.test"))["found"] == []
    fake_dns.table[("_dmarc.slow.test", "TXT")] = TIMEOUT
    assert (await sr.check_dmarc("slow.test"))["error"] is True


async def test_dmarc_missing_fails(fake_dns):
    t = healthy_table()
    del t[("_dmarc.example.org", "TXT")]
    fake_dns.table.update(t)
    rep = await sr.get_report(lm_with_bounces(), SETTINGS)
    assert by_id(rep)["dmarc"]["status"] == "fail"
    assert by_id(rep)["dkim"]["status"] == "pass"


# ── Rejections ──────────────────────────────────────────────────────────────

def _bounce(email, reason, days_ago=0, code="5.7.1"):
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"email": email, "type": "soft", "created_at": when.isoformat().replace("+00:00", "Z"),
            "meta": {"classify_reason": reason, "smtp_code": code}, "campaign": None}


async def test_spam_rejections_are_counted_per_provider(fake_dns):
    fake_dns.table.update(healthy_table())
    lm = lm_with_bounces([
        _bounce("a@gmail.com", "soft: reputation_policy"),
        _bounce("b@gmail.com", "soft: reputation_policy"),
        _bounce("c@yahoo.co.uk", "soft: reverse_dns", code="5.7.25"),
        _bounce("d@x.com", "soft: mailbox_full"),             # not a sender problem
        _bounce("e@gmail.com", "soft: reputation_policy", days_ago=9),   # outside the window
    ])
    rep = await sr.get_report(lm, SETTINGS)
    rej = rep["rejections"]
    assert rej["total"] == 3 and rej["config_total"] == 1
    assert rej["by_provider"] == {"Gmail": 2, "Yahoo / AOL": 1}
    assert by_id(rep)["sender_rejections"]["status"] == "fail"     # reverse DNS = configuration
    assert any(a["tone"] == "danger" and "rejected" in a["text"] for a in sr.alerts(rep))


async def test_reputation_only_rejections_warn(fake_dns):
    fake_dns.table.update(healthy_table())
    rep = await sr.get_report(lm_with_bounces([_bounce("a@gmail.com", "soft: reputation_policy")]), SETTINGS)
    assert by_id(rep)["sender_rejections"]["status"] == "warn"


async def test_bounce_history_failure_is_info(fake_dns):
    fake_dns.table.update(healthy_table())
    lm = lm_with_bounces()
    lm.get_bounces = AsyncMock(side_effect=RuntimeError("down"))
    rep = await sr.get_report(lm, SETTINGS)
    assert by_id(rep)["sender_rejections"]["status"] == "info"


# ── Cache ───────────────────────────────────────────────────────────────────

async def test_report_is_cached_until_refresh(fake_dns):
    fake_dns.table.update(healthy_table())
    lm = lm_with_bounces()
    await sr.get_report(lm, SETTINGS)
    n = len(fake_dns.calls)
    await sr.get_report(lm, SETTINGS)
    assert len(fake_dns.calls) == n + 1          # only the SMTP host is re-resolved
    await sr.get_report(lm, SETTINGS, refresh=True)
    assert len(fake_dns.calls) > n + 10


# ── Bounce classifier: sender-side reasons ──────────────────────────────────

YAHOO_RDNS = ("SMTP error from remote mail server after pipelined MAIL FROM:<sender@example.org>: "
              "550 5.7.25 Forward-confirmed reverse DNS check failed")
MS_BLOCK = ("550 5.7.1 Unfortunately, messages from [66.212.16.31] weren't sent. Please contact your "
            "Internet service provider since part of their network is on our block list (S3150).")
GMAIL_SPAM = ("550-5.7.1 [66.212.16.30 12] Gmail has detected that this message is likely "
              "unsolicited mail. To reduce the amount of spam sent to Gmail, this message has been blocked.")
DMARC = "550 5.7.26 Unauthenticated email from example.com is not accepted due to domain's DMARC policy."


@pytest.mark.parametrize("text, reason", [
    (YAHOO_RDNS, "soft: reverse_dns"),
    (MS_BLOCK, "soft: blacklist"),
    (GMAIL_SPAM, "soft: reputation_policy"),
    (DMARC, "soft: auth_failed"),
])
def test_classifier_names_sender_problems(text, reason):
    result = classify_bounce(text, "Mail delivery failed")
    assert result["type"] == "soft" and result["reason"] == reason


def test_returned_headers_do_not_look_like_auth_failures():
    # A DSN returns the original headers: DKIM-Signature / Received-SPF must
    # not turn a full mailbox into an authentication failure.
    body = ("452 4.2.2 The email account that you tried to reach is over quota\n\n"
            "DKIM-Signature: v=1; a=rsa-sha256; d=example.com\nReceived-SPF: pass (spf check passed)\n")
    assert classify_bounce(body)["reason"] == "soft: quota_exceeded"


# ── API ─────────────────────────────────────────────────────────────────────

def test_reputation_endpoint(fake_dns):
    fake_dns.table.update(healthy_table())
    with patch("app.main.verify_session", return_value=True), \
            patch("app.routers.deliverability.listmonk", lm_with_bounces()):
        client = CSRFTestClient(app, raise_server_exceptions=False)
        res = client.get("/api/deliverability/reputation?refresh=true")
    body = res.json()
    assert res.status_code == 200
    assert body["report"]["from_domain"] == "example.org"
    assert {c["id"] for c in body["checks"]} >= {"reverse_dns", "ip_blocklists", "spf", "dmarc"}
    assert body["alerts"] == []
