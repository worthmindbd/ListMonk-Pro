"""Email validation: address checks, subscriber scans, scheduled cleanup, API.

DNS is replaced by a table-driven fake (``ev._resolve`` is the single seam) and
ListMonk by a small double, so nothing here touches the network. Time is
patched (``email_cleanup._now``) or passed in — never slept.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import dns.exception
import dns.resolver
import pytest

from app.services import email_cleanup as ec
from app.services import email_validation as ev
from app.services.listmonk_client import ListMonkClient
from tests.conftest import CSRFTestClient

UTC = ZoneInfo("UTC")

# The real DNS seam, captured before the autouse fixture swaps it for a fake:
# the fallback-resolver tests exercise it against scripted resolver objects.
REAL_RESOLVE = ev._resolve


# ── Doubles ──────────────────────────────────────────────────────────────────

class MX:
    def __init__(self, exchange):
        self.exchange = exchange


def install_dns(monkeypatch, table=None, calls=None):
    """Replace the DNS seam. ``table`` maps domain -> behaviour (default ok)."""
    table = table or {}

    async def fake(domain, rdtype):
        if calls is not None:
            calls.append((domain, rdtype))
        mode = table.get(domain, "ok")
        if mode == "ok":
            return [MX("mx.test.")]
        if mode == "nxdomain":
            raise dns.resolver.NXDOMAIN()
        if mode == "timeout":
            raise dns.exception.Timeout()
        if mode == "servfail":
            raise dns.resolver.NoNameservers()
        if mode == "oserror":
            raise OSError("network is unreachable")
        if mode == "nullmx":
            return [MX(".")]
        if mode == "nomx":
            raise dns.resolver.NoAnswer()
        if mode == "a_only":
            if rdtype == "MX":
                raise dns.resolver.NoAnswer()
            if rdtype == "A":
                return [object()]
            raise dns.resolver.NoAnswer()
        raise AssertionError(f"unknown mode {mode}")

    monkeypatch.setattr(ev, "_resolve", fake)


class FakeListMonk:
    """The few ListMonk calls scans and cleanups use."""

    iter_pages = ListMonkClient.iter_pages
    paginate_all = ListMonkClient.paginate_all

    def __init__(self, subscribers=None, bounces=None, openers=None, fail_blocklist=False):
        self.subscribers = subscribers or []
        self.bounces = bounces or []
        self.openers = openers or {}          # campaign id -> set of emails
        self.fail_blocklist = fail_blocklist
        self.events: list[str] = []
        self.blocklisted: list[list[int]] = []
        self.queries: list[tuple] = []

    async def get_subscribers(self, page=1, per_page=50, query="", list_id=None):
        if "campaign_views" in query:
            cid = int(query.split("campaign_id=")[1].split(")")[0])
            rows = [s for s in self.subscribers if s["email"] in self.openers.get(cid, set())]
        else:
            self.events.append("get_subscribers")
            self.queries.append((page, per_page, query, list_id))
            rows = [s for s in self.subscribers
                    if s.get("status", "enabled") == "enabled"
                    and (list_id is None or list_id in s.get("list_ids", []))]
        start = (page - 1) * per_page
        return {"data": {"results": rows[start:start + per_page], "total": len(rows)}}

    async def get_bounces(self, page=1, per_page=50, campaign_id=None, source="", bounce_type=""):
        rows = [b for b in self.bounces if not bounce_type or b["type"] == bounce_type]
        start = (page - 1) * per_page
        return {"data": {"results": rows[start:start + per_page], "total": len(rows)}}

    async def blocklist_subscribers(self, ids):
        self.events.append("blocklist")
        if self.fail_blocklist:
            raise RuntimeError("ListMonk is down")
        self.blocklisted.append(list(ids))
        return {"data": True}

    def all_blocklisted(self):
        return sorted(i for chunk in self.blocklisted for i in chunk)


def sub(sid, email, name="", **extra):
    return {"id": sid, "email": email, "name": name, "status": "enabled", **extra}


@pytest.fixture(autouse=True)
def dns_ok(monkeypatch):
    """Every domain resolves unless a test says otherwise."""
    install_dns(monkeypatch)


@pytest.fixture
def client():
    from app.main import app
    with patch("app.main.verify_session", return_value=True):
        yield CSRFTestClient(app, raise_server_exceptions=False)


def fixed_now(monkeypatch, dt):
    monkeypatch.setattr(ec, "_now", lambda: dt)


async def scan_then_wait(monkeypatch, lm, hours=21):
    """Scan, then move the clock past DNS_CONFIRM_AFTER: a dead domain is only
    blocklisted by a run that long after the scan that first saw it."""
    await ec.run_job(lm, "scan")
    fixed_now(monkeypatch, datetime.now(timezone.utc) + timedelta(hours=hours))


# ── Syntax ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("email", [
    "alice@example.com", "a.b+tag@sub.example.co.uk", "UPPER@Example.COM",
    "  padded@example.com  ", "o'brien@example.ie", "x_y-z@ex-ample.org",
    "müller@bücher.example", "user@xn--bcher-kva.example", "1234@example.io",
])
def test_syntax_accepts_ordinary_addresses(email):
    ok, detail, local, domain = ev.parse_address(email)
    assert ok, detail
    assert local and "." in domain


@pytest.mark.parametrize("email", [
    "", "   ", "plain", "@example.com", "alice@", "alice@@example.com",
    "a@b@example.com", "alice@localhost", "alice@example", "alice@.example.com",
    "alice@example..com", "alice@-example.com", "alice@example-.com",
    "alice@exa mple.com", "ali ce@example.com", ".alice@example.com",
    "alice.@example.com", "al..ice@example.com", "alice@example.c",
    "alice@example.123", "alice@[127.0.0.1]", "alice@exam\nple.com", "ali\x00ce@example.com",
    "a" * 65 + "@example.com", "alice@" + "a" * 64 + ".com",
    "a@" + ("b" * 60 + ".") * 5 + "com",
])
def test_syntax_rejects_malformed_addresses(email):
    ok, detail, _, _ = ev.parse_address(email)
    assert not ok and detail


def test_total_length_is_capped_at_254():
    long_local = "a" * 64
    ok, _, _, _ = ev.parse_address(f"{long_local}@{'b' * 63}.{'c' * 63}.{'d' * 60}.com")
    assert not ok


async def test_bad_syntax_never_reaches_dns(monkeypatch):
    calls = []
    install_dns(monkeypatch, calls=calls)
    result = await ev.validate_email("nope@@x")
    assert result["verdict"] == "invalid" and result["reasons"] == ["syntax"]
    assert result["detail"]
    assert calls == []


async def test_non_string_input_is_invalid_not_a_crash():
    result = await ev.validate_email(None)
    assert result["verdict"] == "invalid" and result["reasons"] == ["syntax"]


# ── DNS classification ───────────────────────────────────────────────────────

@pytest.mark.parametrize("mode, verdict, reason", [
    ("ok", "valid", None),
    ("a_only", "valid", None),                 # RFC 5321 implicit MX
    ("nxdomain", "invalid", "no_domain"),
    ("nomx", "invalid", "no_mx"),
    ("nullmx", "invalid", "null_mx"),          # RFC 7505
])
async def test_definitive_dns_answers(monkeypatch, mode, verdict, reason):
    install_dns(monkeypatch, {"probe.test": mode})
    result = await ev.validate_email("someone@probe.test")
    assert result["verdict"] == verdict
    assert (result["reasons"][0] if result["reasons"] else None) == reason


@pytest.mark.parametrize("mode", ["timeout", "servfail", "oserror"])
async def test_transient_dns_failure_is_unknown_never_invalid(monkeypatch, mode):
    """The rule everything else relies on: a flaky network must not look dead."""
    install_dns(monkeypatch, {"probe.test": mode})
    result = await ev.validate_email("someone@probe.test")
    assert result["verdict"] == "unknown"
    assert result["reasons"] == ["dns_unavailable"]


async def test_transient_failure_while_probing_address_records_is_unknown(monkeypatch):
    """No MX, then the A lookup times out: we do not know, so we do not condemn."""
    async def fake(domain, rdtype):
        if rdtype == "MX":
            raise dns.resolver.NoAnswer()
        raise dns.exception.Timeout()
    monkeypatch.setattr(ev, "_resolve", fake)
    assert (await ev.validate_email("a@probe.test"))["verdict"] == "unknown"


async def test_mx_with_a_real_target_beats_a_null_mx_neighbour(monkeypatch):
    async def fake(domain, rdtype):
        return [MX("."), MX("mail.real.test.")]
    monkeypatch.setattr(ev, "_resolve", fake)
    assert (await ev.validate_email("a@probe.test"))["verdict"] == "valid"


# ── Risk flags ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("email, reason", [
    ("someone@mailinator.com", "disposable"),
    ("someone@deep.sub.mailinator.com", "disposable"),
    ("sales@acme.test", "role"),
    ("Support+tag@acme.test", "role"),
    ("jane@gmial.com", "typo"),
    ("jane@gmaail.com", "typo"),           # doubled letter
    ("jane@gmeil.com", "typo"),            # curated substitution typo
    ("jane@hotmail.con", "typo"),
    ("jane@acme.cmo", "typo"),             # TLD slip
])
async def test_risk_flags_give_the_risky_verdict(email, reason):
    result = await ev.validate_email(email)
    assert result["verdict"] == "risky"
    assert reason in result["reasons"]


@pytest.mark.parametrize("email", [
    "info@acme.test", "Info@acme.test", "info+offers@acme.test",
    "hello@acme.test", "HELLO@acme.test", "hello+hi@acme.test",
])
async def test_info_and_hello_are_ordinary_inboxes_not_role_accounts(email):
    # For the small businesses a cold list targets these are the owner's real
    # inbox, so they must never be reported as risky.
    result = await ev.validate_email(email)
    assert result["verdict"] == "valid", result
    assert "role" not in result["reasons"]
    assert not ev.is_role_account(email.split("@")[0].lower())


async def test_typo_offers_the_corrected_address():
    result = await ev.validate_email("jane@gmial.com")
    assert result["suggestion"] == "jane@gmail.com"
    assert "gmail.com" in result["detail"]


@pytest.mark.parametrize("email", [
    "jane@gmail.com", "jane@yahoo.com", "jane@msn.com", "jane@mn.com",
    "jane@email.com", "jane@acme.test", "jane@proton.me",
])
async def test_real_domains_are_not_flagged_as_typos(email):
    result = await ev.validate_email(email)
    assert result["verdict"] == "valid", result


async def test_a_dead_typo_domain_is_invalid_and_still_suggests_the_fix(monkeypatch):
    install_dns(monkeypatch, {"gmial.com": "nxdomain"})
    result = await ev.validate_email("jane@gmial.com")
    assert result["verdict"] == "invalid"
    assert result["reasons"][:2] == ["no_domain", "typo"]
    assert result["suggestion"] == "jane@gmail.com"


async def test_risky_address_on_a_flaky_resolver_stays_risky(monkeypatch):
    install_dns(monkeypatch, {"mailinator.com": "timeout"})
    result = await ev.validate_email("a@mailinator.com")
    assert result["verdict"] == "risky"
    assert "dns_unavailable" in result["reasons"]


@pytest.mark.parametrize("a, b, expected", [
    ("gmial.com", "gmail.com", True),                     # swapped letters
    ("gmal.com", "gmail.com", True),                      # dropped letter
    ("gmaill.com", "gmail.com", True),                    # doubled letter
    ("gmail.com", "gmail.com", False),                    # identical
    ("email.com", "gmail.com", False),                    # substitution: a different real domain
    ("gmeil.com", "gmail.com", False),                    # substitution (curated table instead)
    ("abc", "abcde", False), ("gmxil.net", "gmail.com", False),
])
def test_typing_slip_helper(a, b, expected):
    assert ev._is_typing_slip(a, b) is expected


# ── Cache and concurrency ────────────────────────────────────────────────────

async def test_domain_is_resolved_once_for_many_addresses(monkeypatch):
    calls = []
    install_dns(monkeypatch, calls=calls)
    results = await ev.validate_many([f"user{i}@shared.test" for i in range(30)])
    assert all(r["verdict"] == "valid" for r in results)
    assert calls == [("shared.test", "MX")]


async def test_definitive_result_is_cached_and_invalidate_clears_it(monkeypatch):
    calls = []
    install_dns(monkeypatch, {"gone.test": "nxdomain"}, calls=calls)
    await ev.validate_email("a@gone.test")
    await ev.validate_email("b@gone.test")
    assert len(calls) == 1
    ev.invalidate()
    await ev.validate_email("c@gone.test")
    assert len(calls) == 2


async def test_unknown_result_is_retried_soon_not_cached_for_an_hour(monkeypatch):
    calls = []
    install_dns(monkeypatch, {"flaky.test": "timeout"}, calls=calls)
    await ev.validate_email("a@flaky.test")
    assert ev.UNKNOWN_TTL < ev.DOMAIN_TTL
    key = "flaky.test"
    expires, _ = ev._domain_cache[key]
    # Pretend the short TTL elapsed; the next check must query again.
    ev._domain_cache[key] = (0.0, ev._domain_cache[key][1])
    await ev.validate_email("b@flaky.test")
    assert len(calls) == 2


async def test_cache_stays_bounded(monkeypatch):
    monkeypatch.setattr(ev, "MAX_CACHE_ENTRIES", 20)
    await ev.validate_many([f"u@d{i}.test" for i in range(60)])
    assert len(ev._domain_cache) <= 20 + 1


async def test_unexpected_exception_in_a_lookup_becomes_unknown(monkeypatch):
    async def boom(domain, rdtype):
        raise RuntimeError("socket exploded")
    monkeypatch.setattr(ev, "_resolve", boom)
    # A raw RuntimeError is not a DNSException: still never "invalid".
    result = await ev.validate_email("a@probe.test")
    assert result["verdict"] == "unknown"


async def test_resolver_health_probe(monkeypatch):
    assert await ev.resolver_healthy() is True
    install_dns(monkeypatch, {"gmail.com": "timeout", "outlook.com": "servfail"})
    assert await ev.resolver_healthy() is False


# ── Fallback resolvers ───────────────────────────────────────────────────────

class FakeResolver:
    """Stands in for ``dns.asyncresolver.Resolver``: one scripted behaviour.

    ``mode`` is a string (as in ``install_dns``) or a ``{rdtype: mode}`` dict.
    """

    def __init__(self, mode):
        self.mode = mode
        self.calls = []

    async def resolve(self, domain, rdtype, raise_on_no_answer=True):
        self.calls.append((domain, rdtype))
        mode = self.mode.get(rdtype, "nomx") if isinstance(self.mode, dict) else self.mode
        if mode == "ok":
            return [MX("mx.test.")]
        if mode == "a_ok":
            return [object()]
        if mode == "nxdomain":
            raise dns.resolver.NXDOMAIN()
        if mode == "nomx":
            raise dns.resolver.NoAnswer()
        if mode == "timeout":
            raise dns.exception.Timeout()
        if mode == "servfail":
            raise dns.resolver.NoNameservers()
        if mode == "oserror":
            raise OSError("network is unreachable")
        raise AssertionError(f"unknown mode {mode}")


def wire(monkeypatch, primary, *fallbacks):
    """Run the REAL ``_resolve`` over scripted system + fallback resolvers."""
    monkeypatch.setattr(ev, "_resolve", REAL_RESOLVE)
    monkeypatch.setattr(ev, "_get_resolver", lambda: primary)
    monkeypatch.setattr(ev, "_get_fallback_resolvers", lambda: list(fallbacks))


@pytest.mark.parametrize("failure", ["timeout", "servfail", "oserror"])
async def test_fallback_answers_when_the_system_resolver_cannot(monkeypatch, failure):
    """The motivating case: SERVFAIL / timeout locally, fine on public DNS."""
    primary, fb = FakeResolver(failure), FakeResolver("ok")
    wire(monkeypatch, primary, fb)
    result = await ev.validate_email("someone@flaky.test")
    assert result["verdict"] == "valid"
    assert primary.calls == [("flaky.test", "MX")]
    assert fb.calls == [("flaky.test", "MX")]


async def test_fallback_is_not_consulted_when_the_system_resolver_answers(monkeypatch):
    primary, fb = FakeResolver("ok"), FakeResolver("nxdomain")
    wire(monkeypatch, primary, fb)
    assert (await ev.validate_email("a@fine.test"))["verdict"] == "valid"
    assert fb.calls == []


@pytest.mark.parametrize("mode,verdict,reason", [
    ("nxdomain", "invalid", "no_domain"),
    ("nomx", "invalid", "no_mx"),
])
async def test_a_definitive_local_answer_is_final_not_second_guessed(
        monkeypatch, mode, verdict, reason):
    """NXDOMAIN / NoAnswer are answers: the fallback is only for 'no answer'."""
    primary = FakeResolver({"MX": mode, "A": "nxdomain" if mode == "nxdomain" else "nomx",
                            "AAAA": "nomx"})
    fb = FakeResolver("ok")
    wire(monkeypatch, primary, fb)
    result = await ev.validate_email("a@gone.test")
    assert (result["verdict"], result["reasons"][0]) == (verdict, reason)
    assert fb.calls == []


async def test_a_definitive_answer_from_the_fallback_counts(monkeypatch):
    """Local SERVFAIL, public resolver says the name does not exist: invalid."""
    wire(monkeypatch, FakeResolver("servfail"), FakeResolver("nxdomain"))
    result = await ev.validate_email("a@gone.test")
    assert (result["verdict"], result["reasons"]) == ("invalid", ["no_domain"])


async def test_every_resolver_failing_is_unknown_never_invalid(monkeypatch):
    primary, fb1, fb2 = FakeResolver("servfail"), FakeResolver("timeout"), FakeResolver("oserror")
    wire(monkeypatch, primary, fb1, fb2)
    result = await ev.validate_email("a@dark.test")
    assert (result["verdict"], result["reasons"]) == ("unknown", ["dns_unavailable"])
    # ...and each fallback really was tried.
    assert len(fb1.calls) == len(fb2.calls) == 1


async def test_fallbacks_are_tried_in_order_until_one_answers(monkeypatch):
    primary, fb1, fb2, fb3 = (FakeResolver("servfail"), FakeResolver("timeout"),
                              FakeResolver("ok"), FakeResolver("ok"))
    wire(monkeypatch, primary, fb1, fb2, fb3)
    assert (await ev.validate_email("a@slow.test"))["verdict"] == "valid"
    assert (len(fb1.calls), len(fb2.calls), len(fb3.calls)) == (1, 1, 0)


async def test_without_fallbacks_behaviour_is_unchanged(monkeypatch):
    wire(monkeypatch, FakeResolver("servfail"))
    assert (await ev.validate_email("a@dark.test"))["verdict"] == "unknown"


async def test_fallback_also_covers_the_address_record_probe(monkeypatch):
    """No MX locally, then the A lookup SERVFAILs: public DNS settles it."""
    primary = FakeResolver({"MX": "nomx", "A": "servfail", "AAAA": "nomx"})
    fb = FakeResolver({"MX": "nomx", "A": "a_ok", "AAAA": "nomx"})
    wire(monkeypatch, primary, fb)
    assert (await ev.validate_email("a@implicit.test"))["verdict"] == "valid"
    assert fb.calls == [("implicit.test", "A")]


async def test_health_probe_passes_when_only_the_fallback_works(monkeypatch):
    """DNS 'works' if any configured resolver answers: results are trustworthy."""
    wire(monkeypatch, FakeResolver("servfail"), FakeResolver("ok"))
    assert await ev.resolver_healthy() is True
    wire(monkeypatch, FakeResolver("servfail"), FakeResolver("timeout"))
    assert await ev.resolver_healthy() is False


async def test_cancellation_is_not_swallowed_by_the_fallback(monkeypatch):
    class Hang(FakeResolver):
        async def resolve(self, *a, **k):
            await asyncio.sleep(3600)
    fb = FakeResolver("ok")
    wire(monkeypatch, Hang("x"), fb)
    task = asyncio.ensure_future(ev._resolve("a.test", "MX"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fb.calls == []


def test_fallback_resolvers_are_built_from_the_setting_in_order(monkeypatch):
    monkeypatch.setattr(ev.settings, "email_validation_fallback_dns", ("1.1.1.1", "9.9.9.9"))
    monkeypatch.setattr(ev.settings, "email_validation_dns_timeout", 3)
    ev.invalidate()
    built = ev._get_fallback_resolvers()
    assert [r.nameservers for r in built] == [["1.1.1.1"], ["9.9.9.9"]]
    assert all(r.timeout == 3 and r.lifetime == 3 for r in built)
    assert ev._get_fallback_resolvers() is built          # built once...
    ev.invalidate()
    assert ev._get_fallback_resolvers() is not built      # ...and reset by invalidate()


def test_empty_setting_means_no_fallback(monkeypatch):
    monkeypatch.setattr(ev.settings, "email_validation_fallback_dns", ())
    ev.invalidate()
    assert ev._get_fallback_resolvers() == []


def test_default_fallback_is_cloudflare_then_google():
    from app.config import Settings
    assert Settings.email_validation_fallback_dns == ("1.1.1.1", "8.8.8.8")


@pytest.mark.parametrize("raw,expected", [
    (None, ("1.1.1.1", "8.8.8.8")),                      # unset -> default
    ("", ()),                                            # set but empty -> off
    ("   ", ()),
    ("9.9.9.9", ("9.9.9.9",)),
    ("9.9.9.9, 149.112.112.112", ("9.9.9.9", "149.112.112.112")),
    ("9.9.9.9 149.112.112.112", ("9.9.9.9", "149.112.112.112")),   # spaces work too
    ("2606:4700:4700::1111", ("2606:4700:4700::1111",)),            # IPv6
    ("9.9.9.9,9.9.9.9", ("9.9.9.9",)),                   # de-duplicated
    ("dns.google, 9.9.9.9", ("9.9.9.9",)),               # hostnames dropped
    ("9.9.9.999", ()),                                   # typo: off, NOT the default
    ("none", ()),
])
def test_fallback_setting_parsing(monkeypatch, raw, expected):
    """A typo must never fall back to the default: the list decides which
    third parties see your subscribers' domains."""
    from app.config import _env_ip_list
    if raw is None:
        monkeypatch.delenv("X_TEST_DNS", raising=False)
    else:
        monkeypatch.setenv("X_TEST_DNS", raw)
    assert _env_ip_list("X_TEST_DNS", "1.1.1.1,8.8.8.8") == expected


# ── Scan ─────────────────────────────────────────────────────────────────────

def mixed_book(monkeypatch):
    install_dns(monkeypatch, {"dead.test": "nxdomain", "slow.test": "timeout"})
    return [
        sub(1, "ok@good.test", "Ok"),
        sub(2, "gone@dead.test", "Gone"),
        sub(3, "who@slow.test", "Who"),
        sub(4, "sales@good.test", "Sales"),
        sub(5, "bad@@x", "Syntax"),
        sub(6, "temp@mailinator.com", "Temp"),
    ]


async def test_scan_counts_and_stores_only_the_problems(monkeypatch):
    lm = FakeListMonk(mixed_book(monkeypatch))
    summary = await ec.run_job(lm, "scan")

    assert summary["status"] == "ok"
    assert (summary["checked"], summary["valid"], summary["invalid"],
            summary["risky"], summary["unknown"]) == (6, 1, 2, 2, 1)
    stored = ec.load_results()
    assert set(stored) == {"gone@dead.test", "who@slow.test", "sales@good.test",
                           "bad@@x", "temp@mailinator.com"}
    assert "ok@good.test" not in stored                      # valid: counted, not stored
    assert stored["gone@dead.test"]["subscriber_id"] == 2
    assert stored["gone@dead.test"]["reasons"] == ["no_domain"]
    assert ec.result_counts() == {"all": 5, "invalid": 2, "risky": 2, "unknown": 1}


async def test_scan_only_reads_enabled_subscribers_and_never_blocklists(monkeypatch):
    book = mixed_book(monkeypatch) + [{**sub(9, "old@dead.test"), "status": "blocklisted"}]
    lm = FakeListMonk(book)
    await ec.run_job(lm, "scan")

    assert all(q[2] == "subscribers.status = 'enabled'" for q in lm.queries)
    assert "old@dead.test" not in ec.load_results()
    assert lm.blocklisted == []


async def test_scan_walks_every_page(monkeypatch):
    monkeypatch.setattr(ec, "PAGE_SIZE", 2)
    lm = FakeListMonk(mixed_book(monkeypatch))
    summary = await ec.run_job(lm, "scan")
    assert summary["checked"] == 6
    assert [q[0] for q in lm.queries] == [1, 2, 3]


async def test_scan_can_be_limited_to_one_list(monkeypatch):
    install_dns(monkeypatch, {"dead.test": "nxdomain"})
    lm = FakeListMonk([
        sub(1, "a@dead.test", list_ids=[5]),
        sub(2, "b@dead.test", list_ids=[6]),
    ])
    summary = await ec.run_job(lm, "scan", list_id=5)
    assert summary["checked"] == 1
    assert set(ec.load_results()) == {"a@dead.test"}
    assert lm.queries[0][3] == 5


async def test_full_scan_replaces_the_store_but_a_list_scan_keeps_other_entries(monkeypatch):
    install_dns(monkeypatch, {"dead.test": "nxdomain"})
    lm = FakeListMonk([
        sub(1, "a@dead.test", list_ids=[5]),
        sub(2, "b@dead.test", list_ids=[6]),
    ])
    await ec.run_job(lm, "scan")
    assert set(ec.load_results()) == {"a@dead.test", "b@dead.test"}

    # Subscriber 1 is fixed; a scan of list 5 clears only their entry.
    lm.subscribers[0]["email"] = "a@good.test"
    await ec.run_job(lm, "scan", list_id=5)
    assert set(ec.load_results()) == {"b@dead.test"}

    # A full scan forgets entries for subscribers that no longer exist.
    lm.subscribers = [sub(3, "c@dead.test")]
    await ec.run_job(lm, "scan")
    assert set(ec.load_results()) == {"c@dead.test"}


async def test_hard_bounced_subscribers_are_invalid_openers_are_not(monkeypatch):
    lm = FakeListMonk(
        [sub(1, "bounced@good.test"), sub(2, "opened@good.test"), sub(3, "fine@good.test")],
        bounces=[
            {"type": "hard", "email": "bounced@good.test", "subscriber_id": 1,
             "campaign": {"id": 7, "name": "C"}},
            {"type": "hard", "email": "opened@good.test", "subscriber_id": 2,
             "campaign": {"id": 7, "name": "C"}},
            {"type": "soft", "email": "fine@good.test", "subscriber_id": 3,
             "campaign": {"id": 7, "name": "C"}},
        ],
        openers={7: {"opened@good.test"}},
    )
    await ec.run_job(lm, "scan")
    stored = ec.load_results()
    assert set(stored) == {"bounced@good.test"}
    assert stored["bounced@good.test"]["verdict"] == "invalid"
    assert stored["bounced@good.test"]["reasons"] == ["hard_bounce"]


async def test_bounce_without_a_subscriber_id_matches_by_email(monkeypatch):
    lm = FakeListMonk(
        [sub(1, "bounced@good.test")],
        bounces=[{"type": "hard", "email": "Bounced@Good.test", "campaign": None}],
    )
    await ec.run_job(lm, "scan")
    assert ec.load_results()["bounced@good.test"]["reasons"] == ["hard_bounce"]


async def test_scan_fails_cleanly_when_dns_is_down(monkeypatch):
    install_dns(monkeypatch, {"gmail.com": "timeout", "outlook.com": "timeout"})
    lm = FakeListMonk([sub(1, "a@good.test")])
    summary = await ec.run_job(lm, "scan")
    assert summary["status"] == "failed" and "DNS" in summary["message"]
    assert lm.queries == []
    assert ec.load_results() == {}


async def test_job_slot_is_released_and_summary_recorded_after_a_crash(monkeypatch):
    class Boom(FakeListMonk):
        async def get_subscribers(self, *a, **k):
            raise RuntimeError("ListMonk exploded")
    summary = await ec.run_job(Boom([]), "scan")
    assert summary["status"] == "failed" and "exploded" in summary["message"]
    assert ec.job_snapshot()["running"] is False
    assert ec.load_state()["last_scan"]["status"] == "failed"
    ec._begin_job("scan", None)           # slot is free again


async def test_only_one_job_at_a_time():
    ec._begin_job("scan", None)
    with pytest.raises(ec.JobBusy):
        ec._begin_job("cleanup", None)
    assert ec.request_cancel() is True
    assert ec.job_snapshot()["cancel_requested"] is True


async def test_cancel_stops_after_the_current_page_and_never_cleans(monkeypatch):
    monkeypatch.setattr(ec, "PAGE_SIZE", 2)
    install_dns(monkeypatch, {"dead.test": "nxdomain"})
    book = [sub(i, f"u{i}@dead.test") for i in range(1, 9)]

    class CancelAfterFirstPage(FakeListMonk):
        async def get_subscribers(self, *a, **k):
            result = await super().get_subscribers(*a, **k)
            ec.request_cancel()
            return result

    lm = CancelAfterFirstPage(book)
    ec._begin_job("cleanup", None)
    summary = await ec._execute(lm, "cleanup", None)
    assert summary["status"] == "cancelled"
    assert summary["checked"] == 2
    assert lm.blocklisted == []


# ── Cleanup ──────────────────────────────────────────────────────────────────

def seed_state(**over):
    state = ec.load_state()
    state.update(over)
    ec.save_state(state)


async def test_cleanup_blocklists_dead_addresses_only(monkeypatch):
    lm = FakeListMonk(mixed_book(monkeypatch))
    await scan_then_wait(monkeypatch, lm)  # a dead domain must stay dead for a day
    summary = await ec.run_job(lm, "cleanup")

    # 2 = dead domain, 5 = bad syntax. Role (4), disposable (6), unknown (3) stay.
    assert lm.all_blocklisted() == [2, 5]
    assert summary["status"] == "ok" and summary["blocklisted"] == 2
    assert set(ec.load_results()) == {"who@slow.test", "sales@good.test", "temp@mailinator.com"}
    cleaned = ec.load_state()["recent_cleaned"]
    assert {c["email"] for c in cleaned} == {"gone@dead.test", "bad@@x"}
    assert ec.load_state()["last_run"]["blocklisted"] == 2


async def test_cleanup_waits_for_a_second_scan_before_blocklisting_a_dead_domain(monkeypatch):
    """One NXDOMAIN is one DNS answer (registrar hold, a botched zone for a
    day); a blocklisting is permanent. Syntax errors and hard bounces are
    facts and are acted on at once."""
    book = [sub(1, "gone@dead.test"), sub(2, "bounced@good.test"), sub(3, "bad@@x")]
    bounces = [{"type": "hard", "email": "bounced@good.test", "subscriber_id": 2,
                "campaign": None}]
    install_dns(monkeypatch, {"dead.test": "nxdomain"})

    lm = FakeListMonk(book, bounces)
    first = await ec.run_job(lm, "cleanup")
    assert lm.all_blocklisted() == [2, 3]
    assert first["pending_confirmation"] == 1
    assert "gone@dead.test" in ec.load_results()        # kept for the next run

    lm = FakeListMonk([sub(1, "gone@dead.test")])
    fixed_now(monkeypatch, datetime.now(timezone.utc) + timedelta(hours=21))
    second = await ec.run_job(lm, "cleanup")
    assert lm.all_blocklisted() == [1]
    assert second["pending_confirmation"] == 0


async def test_cleanup_honours_the_criteria_toggles(monkeypatch):
    book = [sub(1, "gone@dead.test"), sub(2, "bounced@good.test")]
    bounces = [{"type": "hard", "email": "bounced@good.test", "subscriber_id": 2,
                "campaign": None}]
    install_dns(monkeypatch, {"dead.test": "nxdomain"})

    seed_state(clean_invalid=True, clean_hard_bounces=False)
    lm = FakeListMonk(book, bounces)
    await scan_then_wait(monkeypatch, lm)  # a dead domain must stay dead for a day
    await ec.run_job(lm, "cleanup")
    assert lm.all_blocklisted() == [1]

    seed_state(clean_invalid=False, clean_hard_bounces=True)
    lm = FakeListMonk(book, bounces)
    await ec.run_job(lm, "cleanup")
    assert lm.all_blocklisted() == [2]


async def test_cleanup_never_acts_on_unknown_or_risky(monkeypatch):
    install_dns(monkeypatch, {"slow.test": "timeout"})
    lm = FakeListMonk([sub(1, "a@slow.test"), sub(2, "sales@good.test"),
                       sub(3, "x@mailinator.com"), sub(4, "x@gmial.com")])
    summary = await ec.run_job(lm, "cleanup")
    assert lm.blocklisted == []
    assert summary["candidates"] == 0 and summary["message"] == "Nothing to clean."


async def test_cleanup_aborts_untouched_when_dns_is_broken(monkeypatch):
    install_dns(monkeypatch, {"gmail.com": "servfail", "outlook.com": "servfail"})
    lm = FakeListMonk([sub(1, "a@dead.test")])
    summary = await ec.run_job(lm, "cleanup")
    assert summary["status"] == "failed"
    assert lm.blocklisted == [] and lm.queries == []


def crowd(monkeypatch, dead, alive):
    install_dns(monkeypatch, {"dead.test": "nxdomain"})
    return ([sub(i, f"d{i}@dead.test") for i in range(dead)]
            + [sub(1000 + i, f"a{i}@good.test") for i in range(alive)])


async def test_cleanup_is_held_when_it_would_remove_too_much(monkeypatch):
    lm = FakeListMonk(crowd(monkeypatch, dead=13, alive=47))     # 21.7 % of 60
    summary = await ec.run_job(lm, "cleanup")
    assert summary["status"] == "held"
    assert summary["candidates"] == 13 and summary["blocklisted"] == 0
    assert lm.blocklisted == []
    assert "review" in summary["message"].lower()
    assert len(ec.load_results()) == 13                    # visible for review


async def test_cleanup_at_the_threshold_still_runs(monkeypatch):
    lm = FakeListMonk(crowd(monkeypatch, dead=12, alive=48))     # exactly 20 %
    await scan_then_wait(monkeypatch, lm)  # a dead domain must stay dead for a day
    summary = await ec.run_job(lm, "cleanup")
    assert summary["status"] == "ok" and summary["blocklisted"] == 12


async def test_small_lists_are_not_subject_to_the_hold(monkeypatch):
    lm = FakeListMonk(crowd(monkeypatch, dead=3, alive=2))       # 60 % but only 5 checked
    await scan_then_wait(monkeypatch, lm)  # a dead domain must stay dead for a day
    summary = await ec.run_job(lm, "cleanup")
    assert summary["status"] == "ok" and summary["blocklisted"] == 3


async def test_blocklisting_happens_in_chunks_after_every_page_was_read(monkeypatch):
    monkeypatch.setattr(ec, "PAGE_SIZE", 25)
    monkeypatch.setattr(ec, "CLEAN_CHUNK", 4)
    monkeypatch.setattr(ec, "HOLD_MIN_SAMPLE", 10_000)           # disable the hold
    lm = FakeListMonk(crowd(monkeypatch, dead=10, alive=0))
    await scan_then_wait(monkeypatch, lm)  # a dead domain must stay dead for a day
    await ec.run_job(lm, "cleanup")

    assert [len(c) for c in lm.blocklisted] == [4, 4, 2]
    first_blocklist = lm.events.index("blocklist")
    assert "get_subscribers" not in lm.events[first_blocklist:]


async def test_partial_blocklist_failure_is_reported_not_swallowed(monkeypatch):
    lm = FakeListMonk(crowd(monkeypatch, dead=3, alive=0), fail_blocklist=True)
    await scan_then_wait(monkeypatch, lm)  # a dead domain must stay dead for a day
    summary = await ec.run_job(lm, "cleanup")
    assert summary["status"] == "failed"
    assert summary["blocklisted"] == 0 and summary["failed_blocklist"] == 3
    assert "ListMonk is down" in summary["message"]
    assert ec.load_state()["recent_cleaned"] == []         # nothing was cleaned
    assert len(ec.load_results()) == 3                     # still listed for retry


# ── Schedule maths ───────────────────────────────────────────────────────────

def cfg(**over):
    base = ec._default_state()
    base.update(enabled=True, time="03:00", timezone="UTC")
    base.update(over)
    return base


def at(y, m, d, hh=0, mm=0, tz=UTC):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


def test_daily_slots():
    c = cfg(frequency="daily")
    assert ec.latest_slot(c, at(2026, 11, 4, 10)) == at(2026, 11, 4, 3)
    assert ec.latest_slot(c, at(2026, 11, 4, 2)) == at(2026, 11, 3, 3)
    assert ec.next_slot(c, at(2026, 11, 4, 10)) == at(2026, 11, 5, 3)
    assert ec.latest_slot(c, at(2026, 11, 4, 3)) == at(2026, 11, 4, 3)    # exactly on it


def test_weekly_slots():
    c = cfg(frequency="weekly", weekday=6)              # Sunday; 2026-11-04 is a Wednesday
    assert ec.latest_slot(c, at(2026, 11, 4, 10)) == at(2026, 11, 1, 3)
    assert ec.next_slot(c, at(2026, 11, 4, 10)) == at(2026, 11, 8, 3)
    assert ec.latest_slot(c, at(2026, 11, 8, 2)) == at(2026, 11, 1, 3)    # Sunday, before 03:00
    assert ec.latest_slot(c, at(2026, 11, 8, 3)) == at(2026, 11, 8, 3)


def test_monthly_slots_wrap_months_and_years():
    c = cfg(frequency="monthly", day_of_month=15)
    assert ec.latest_slot(c, at(2026, 11, 4)) == at(2026, 10, 15, 3)
    assert ec.next_slot(c, at(2026, 11, 4)) == at(2026, 11, 15, 3)
    assert ec.latest_slot(c, at(2027, 1, 10)) == at(2026, 12, 15, 3)
    assert ec.next_slot(c, at(2026, 12, 20)) == at(2027, 1, 15, 3)
    assert ec.latest_slot(cfg(frequency="monthly", day_of_month=1), at(2026, 3, 1, 4)) == at(2026, 3, 1, 3)


def test_slots_follow_the_configured_timezone():
    dhaka = ZoneInfo("Asia/Dhaka")                      # UTC+6, no DST
    c = cfg(frequency="daily", timezone="Asia/Dhaka")
    now = at(2026, 11, 4, 22)                           # = 04:00 on Nov 5 in Dhaka
    assert ec.latest_slot(c, now) == at(2026, 11, 5, 3, tz=dhaka)
    assert ec.latest_slot(c, now).astimezone(UTC) == at(2026, 11, 4, 21)


def test_slot_keeps_wall_clock_time_across_a_dst_change():
    ny = ZoneInfo("America/New_York")                   # DST ends 2026-11-01
    c = cfg(frequency="daily", timezone="America/New_York")
    before = ec.latest_slot(c, at(2026, 10, 31, 12))
    after = ec.latest_slot(c, at(2026, 11, 2, 12))
    assert (before.hour, after.hour) == (3, 3)
    assert before.utcoffset() != after.utcoffset()
    assert after.tzinfo == ny


def test_is_due_needs_enabled_and_an_unhandled_slot():
    now = at(2026, 11, 4, 10)
    c = cfg(frequency="daily", last_slot=None)
    assert ec.is_due(c, now) is True
    c["last_slot"] = ec._iso(ec.latest_slot(c, now))
    assert ec.is_due(c, now) is False
    assert ec.is_due(c, now + timedelta(days=1)) is True
    assert ec.is_due({**c, "enabled": False, "last_slot": None}, now) is False


async def test_enabling_does_not_fire_for_a_slot_already_in_the_past():
    now = at(2026, 11, 4, 10)
    state = await ec.update_settings({"enabled": True, "frequency": "daily"}, now=now)
    assert ec.is_due(state, now) is False
    assert ec.is_due(state, now + timedelta(hours=18)) is True       # next 03:00


async def test_moving_the_schedule_rebaselines_instead_of_firing():
    now = at(2026, 11, 4, 10)
    await ec.update_settings({"enabled": True, "frequency": "weekly"}, now=now)
    state = await ec.update_settings({"frequency": "daily"}, now=now)   # 03:00 today is past
    assert ec.is_due(state, now) is False


async def test_changing_a_non_schedule_setting_keeps_the_pending_slot():
    now = at(2026, 11, 4, 10)
    seed_state(enabled=True, frequency="daily", last_slot=None)
    state = await ec.update_settings({"clean_hard_bounces": False}, now=now)
    assert state["last_slot"] is None and ec.is_due(state, now) is True


async def test_enabling_with_nothing_to_clean_is_rejected():
    with pytest.raises(ValueError, match="at least one"):
        await ec.update_settings({"enabled": True, "clean_invalid": False,
                                  "clean_hard_bounces": False})
    assert ec.load_state()["enabled"] is False


@pytest.mark.parametrize("bad", [
    {}, [], "x", {"nope": 1}, {"enabled": "true"}, {"enabled": 1},
    {"frequency": "hourly"}, {"time": "3am"}, {"time": "24:00"}, {"time": "03:60"},
    {"time": 300}, {"weekday": 7}, {"weekday": True}, {"weekday": "1"},
    {"day_of_month": 0}, {"day_of_month": 29}, {"day_of_month": False},
    {"timezone": "Mars/Base"}, {"timezone": 3}, {"clean_invalid": None},
])
def test_validate_patch_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        ec.validate_patch(bad)


def test_validate_patch_accepts_a_full_valid_patch():
    patch = {"enabled": True, "frequency": "monthly", "time": "23:59", "weekday": 0,
             "day_of_month": 28, "timezone": "Europe/Berlin",
             "clean_invalid": True, "clean_hard_bounces": False}
    assert ec.validate_patch(patch) == patch


def test_load_state_repairs_a_corrupted_file():
    ec.CLEANUP_FILE.write_text('{"enabled": "yes", "frequency": "hourly", "time": "99:99",'
                               ' "weekday": 12, "timezone": "Nope/Nope", "last_slot": "x",'
                               ' "last_run": 5, "recent_cleaned": "no"}')
    state = ec.load_state()
    default = ec._default_state()
    for key in ("enabled", "frequency", "time", "weekday", "timezone", "last_slot",
                "last_run", "recent_cleaned"):
        assert state[key] == default[key], key
    ec.CLEANUP_FILE.write_text("[]")
    assert ec.load_state() == default
    ec.CLEANUP_FILE.write_text("{ not json")
    assert ec.load_state() == default


# ── Scheduler tick ───────────────────────────────────────────────────────────

async def test_tick_runs_a_due_cleanup_once_then_waits_for_the_next_slot(monkeypatch):
    now = at(2026, 11, 4, 10)
    fixed_now(monkeypatch, now)
    seed_state(enabled=True, frequency="daily", last_slot=None)
    # A malformed address is acted on at once (a dead domain waits for a
    # second scan), which keeps this test about the schedule.
    lm = FakeListMonk([sub(1, "bad@@x"), sub(2, "ok@good.test")])
    install_dns(monkeypatch, {"dead.test": "nxdomain"})

    summary = await ec.tick(lm, now)
    assert summary["status"] == "ok" and lm.all_blocklisted() == [1]
    assert ec.tick.__doc__
    assert await ec.tick(lm, now) is None                      # slot used up

    later = now + timedelta(days=1)
    fixed_now(monkeypatch, later)
    assert (await ec.tick(lm, later))["status"] == "ok"        # next day's slot


async def test_tick_does_nothing_when_disabled_or_not_due(monkeypatch):
    now = at(2026, 11, 4, 10)
    lm = FakeListMonk([sub(1, "gone@dead.test")])
    install_dns(monkeypatch, {"dead.test": "nxdomain"})
    assert await ec.tick(lm, now) is None                      # disabled
    await ec.update_settings({"enabled": True, "frequency": "daily"}, now=now)
    assert await ec.tick(lm, now) is None                      # baselined, not due
    assert lm.queries == [] and lm.blocklisted == []


async def test_tick_skips_while_a_job_is_running(monkeypatch):
    seed_state(enabled=True, frequency="daily", last_slot=None)
    ec._begin_job("scan", None)
    lm = FakeListMonk([sub(1, "gone@dead.test")])
    assert await ec.tick(lm, at(2026, 11, 4, 10)) is None
    assert lm.queries == []


async def test_failed_scheduled_run_keeps_its_slot_and_retries_after_an_hour(monkeypatch):
    now = at(2026, 11, 4, 10)
    fixed_now(monkeypatch, now)
    seed_state(enabled=True, frequency="daily", last_slot=None)
    install_dns(monkeypatch, {"gmail.com": "timeout", "outlook.com": "timeout"})
    lm = FakeListMonk([sub(1, "bad@@x")])

    assert (await ec.tick(lm, now))["status"] == "failed"
    assert ec.load_state()["last_slot"] is None                # still owed

    soon = now + timedelta(minutes=30)
    fixed_now(monkeypatch, soon)
    assert await ec.tick(lm, soon) is None                     # inside the retry window

    install_dns(monkeypatch, {"dead.test": "nxdomain"})
    ec.reset_job()
    later = now + timedelta(hours=1, minutes=1)
    fixed_now(monkeypatch, later)
    assert (await ec.tick(lm, later))["status"] == "ok"
    assert lm.all_blocklisted() == [1]


async def test_a_held_run_still_uses_up_its_slot(monkeypatch):
    now = at(2026, 11, 4, 10)
    fixed_now(monkeypatch, now)
    seed_state(enabled=True, frequency="daily", last_slot=None)
    lm = FakeListMonk(crowd(monkeypatch, dead=30, alive=30))
    assert (await ec.tick(lm, now))["status"] == "held"
    assert await ec.tick(lm, now) is None
    assert lm.blocklisted == []


def test_view_reports_the_next_run_only_when_enabled():
    now = at(2026, 11, 4, 10)
    off = ec.view(ec.load_state(), now)
    assert off["enabled"] is False and off["next_run"] is None
    on = ec.view(cfg(frequency="daily", last_scan=None, last_run=None, recent_cleaned=[]), now)
    assert on["next_run"] == "2026-11-05T03:00:00+00:00"
    assert on["hold_percent"] == 20 and on["job"]["running"] is False


# ── API ──────────────────────────────────────────────────────────────────────

def test_requests_need_the_csrf_header_and_a_session():
    from app.main import app
    from starlette.testclient import TestClient
    with patch("app.main.verify_session", return_value=True):
        r = TestClient(app).post("/api/email-validation/check", content="emails=a@b.co",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 403
    assert TestClient(app).get("/api/email-validation/status").status_code == 401


def test_check_validates_pasted_addresses(client, monkeypatch):
    install_dns(monkeypatch, {"dead.test": "nxdomain"})
    r = client.post("/api/email-validation/check",
                    json={"emails": ["ok@good.test", " gone@dead.test ", "ok@good.test", "bad"]})
    assert r.status_code == 200
    got = {x["email"]: x["verdict"] for x in r.json()["results"]}
    assert got == {"ok@good.test": "valid", "gone@dead.test": "invalid", "bad": "invalid"}


@pytest.mark.parametrize("body", [
    {}, {"emails": "a@b.co"}, {"emails": []}, {"emails": ["", "  "]},
    {"emails": [1, 2]}, {"emails": [f"u{i}@x.test" for i in range(101)]},
])
def test_check_rejects_bad_bodies(client, body):
    assert client.post("/api/email-validation/check", json=body).status_code == 400


def test_status_before_any_scan(client):
    body = client.get("/api/email-validation/status").json()
    assert body["job"]["running"] is False and body["last_scan"] is None
    assert body["counts"] == {"all": 0, "invalid": 0, "risky": 0, "unknown": 0}


def test_scan_starts_a_background_job(client):
    with patch("app.services.email_cleanup.spawn") as spawn:
        r = client.post("/api/email-validation/scan", json={"list_id": 3})
        spawn.call_args[0][0].close()
    assert r.status_code == 200
    assert r.json()["job"]["running"] is True and r.json()["job"]["list_id"] == 3
    assert client.get("/api/email-validation/status").json()["job"]["kind"] == "scan"


def test_scan_while_busy_is_409(client):
    ec._begin_job("scan", None)
    assert client.post("/api/email-validation/scan").status_code == 409
    assert client.post("/api/email-validation/cleanup/run").status_code == 409


@pytest.mark.parametrize("list_id", [0, -1, "5", True, 1.5])
def test_scan_rejects_a_bad_list_id(client, list_id):
    assert client.post("/api/email-validation/scan", json={"list_id": list_id}).status_code == 400
    assert ec.job_snapshot()["running"] is False


def test_cancel_needs_a_running_job(client):
    assert client.post("/api/email-validation/scan/cancel").status_code == 409
    ec._begin_job("scan", None)
    assert client.post("/api/email-validation/scan/cancel").json() == {"status": "cancelling"}


def stored(*items):
    """Seed the results store."""
    async def go():
        async with ec._RESULTS_LOCK:
            await ec._save_results({i["email"]: i for i in items})
    asyncio.run(go())


def item(sid, email, verdict="invalid", reasons=("no_domain",), name=""):
    return {"subscriber_id": sid, "name": name, "email": email, "domain": email.split("@")[-1],
            "verdict": verdict, "reasons": list(reasons), "suggestion": None,
            "detail": "d", "checked_at": "2026-11-04T10:00:00+00:00"}


def test_results_filter_search_and_paginate(client):
    stored(item(1, "a@dead.test"), item(2, "b@dead.test"),
           item(3, "sales@x.test", "risky", ["role"], name="Zed"),
           item(4, "t@slow.test", "unknown", ["dns_unavailable"]))
    get = lambda **p: client.get("/api/email-validation/results", params=p).json()

    everything = get()
    assert everything["total"] == 4 and everything["counts"]["invalid"] == 2
    assert [r["verdict"] for r in everything["results"]] == ["invalid", "invalid", "risky", "unknown"]
    assert get(verdict="risky")["total"] == 1
    assert [r["email"] for r in get(search="ZED")["results"]] == ["sales@x.test"]
    page = get(per_page=2, page=2)
    assert [r["subscriber_id"] for r in page["results"]] == [3, 4]
    assert get(page=9)["results"] == []


@pytest.mark.parametrize("params", [
    {"verdict": "valid"}, {"verdict": "x"}, {"page": 0}, {"per_page": 0},
    {"per_page": 201}, {"page": 100_001}, {"search": "x" * 201},
])
def test_results_reject_out_of_range_input(client, params):
    assert client.get("/api/email-validation/results", params=params).status_code in (400, 422)


def test_export_is_csv_and_defuses_formula_injection(client):
    stored(item(1, "a@dead.test", name='=HYPERLINK("http://evil")'),
           item(2, "b@x.test", "risky", ["role", "typo"]))
    r = client.get("/api/email-validation/results/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.splitlines()
    assert lines[0].startswith("email,name,verdict,reasons")
    assert "'=HYPERLINK" in r.text
    assert "role; typo" in r.text
    only = client.get("/api/email-validation/results/export", params={"verdict": "risky"}).text
    assert "a@dead.test" not in only


def test_export_of_nothing_is_404(client):
    assert client.get("/api/email-validation/results/export").status_code == 404


def test_blocklist_touches_only_the_submitted_rows_and_only_known_ones(client):
    stored(item(1, "a@dead.test"), item(2, "b@dead.test"), item(3, "c@dead.test"))
    lm = FakeListMonk()
    with patch("app.routers.email_validation.listmonk", lm):
        r = client.put("/api/email-validation/results/blocklist", json={"ids": [1, 3, 999]})
    assert r.status_code == 200
    assert r.json() == {"blocklisted": 2, "errors": 0, "failed": [], "skipped": 1}
    assert lm.all_blocklisted() == [1, 3]                       # 2 untouched, 999 refused
    assert set(ec.load_results()) == {"b@dead.test"}


def test_blocklist_reports_partial_failure_and_keeps_the_rows(client):
    stored(item(1, "a@dead.test"))
    with patch("app.routers.email_validation.listmonk", FakeListMonk(fail_blocklist=True)):
        body = client.put("/api/email-validation/results/blocklist", json={"ids": [1]}).json()
    assert body["blocklisted"] == 0 and body["errors"] == 1
    assert body["failed"][0]["id"] == 1 and "down" in body["failed"][0]["error"]
    assert set(ec.load_results()) == {"a@dead.test"}


@pytest.mark.parametrize("body", [{}, {"ids": []}, {"ids": "1"}, {"ids": [0]},
                                  {"ids": [True]}, {"ids": [1.5]}, {"ids": list(range(1, 502))}])
def test_blocklist_validates_ids(client, body):
    assert client.put("/api/email-validation/results/blocklist", json=body).status_code == 400


def test_dismiss_removes_rows_without_touching_listmonk(client):
    stored(item(1, "a@dead.test"), item(2, "b@dead.test"))
    lm = FakeListMonk()
    with patch("app.routers.email_validation.listmonk", lm):
        r = client.delete("/api/email-validation/results", params={"ids": [1]})
    assert r.json() == {"dismissed": 1}
    assert set(ec.load_results()) == {"b@dead.test"} and lm.blocklisted == []
    assert client.delete("/api/email-validation/results").status_code == 422
    assert client.delete("/api/email-validation/results", params={"ids": [0]}).status_code == 400


def test_cleanup_settings_round_trip(client):
    body = client.get("/api/email-validation/cleanup").json()
    assert body["enabled"] is False and body["frequency"] == "weekly" and body["next_run"] is None

    r = client.put("/api/email-validation/cleanup", json={
        "enabled": True, "frequency": "monthly", "day_of_month": 10, "time": "04:30",
        "timezone": "Asia/Dhaka", "clean_hard_bounces": False})
    assert r.status_code == 200
    saved = r.json()["cleanup"]
    assert saved["enabled"] is True and saved["next_run"]
    assert (saved["frequency"], saved["day_of_month"], saved["time"]) == ("monthly", 10, "04:30")
    assert client.get("/api/email-validation/cleanup").json()["clean_hard_bounces"] is False


@pytest.mark.parametrize("bad", [
    {}, {"frequency": "hourly"}, {"nope": 1}, {"enabled": "yes"}, {"day_of_month": 31},
    {"timezone": "Mars/Base"}, {"enabled": True, "clean_invalid": False, "clean_hard_bounces": False},
])
def test_cleanup_settings_reject_bad_input_and_write_nothing(client, bad):
    assert client.put("/api/email-validation/cleanup", json=bad).status_code == 400
    assert not ec.CLEANUP_FILE.exists()


def test_cleanup_settings_write_failure_is_a_500(client):
    with patch("app.services.email_cleanup.save_state", side_effect=OSError("disk")):
        r = client.put("/api/email-validation/cleanup", json={"frequency": "daily"})
    assert r.status_code == 500 and "permissions" in r.json()["detail"]


def test_run_now_starts_a_cleanup_job(client):
    with patch("app.services.email_cleanup.spawn") as spawn:
        r = client.post("/api/email-validation/cleanup/run")
        spawn.call_args[0][0].close()
    assert r.status_code == 200 and r.json()["job"]["kind"] == "cleanup"


def test_run_now_needs_something_to_clean(client):
    seed_state(clean_invalid=False, clean_hard_bounces=False)
    assert client.post("/api/email-validation/cleanup/run").status_code == 400
    assert ec.job_snapshot()["running"] is False
