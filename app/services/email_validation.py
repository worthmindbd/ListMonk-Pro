"""
Email address validation: syntax, DNS/MX and risk flags.

This module is a pure validator. It never talks to ListMonk and never decides
what to do with an address; ``email_cleanup`` builds the scan / cleanup jobs on
top of it.

Verdicts
--------
``invalid``  the address can never receive mail (malformed, the domain does not
             exist, or the domain has no mail server / publishes a Null MX).
``risky``    deliverable in principle but a poor target: a throwaway
             (disposable) domain, a role account (``support@``) or a probable
             typo of a well-known provider (``gmial.com``).
``unknown``  the DNS lookup failed for a *transient* reason (timeout,
             SERVFAIL, resolver unreachable). Says nothing about the address.
``valid``    passed every check.

The one rule that matters
-------------------------
Only a *definitive* DNS answer may produce ``invalid``. A timeout or a
resolver error is ``unknown``, always. Callers can blocklist on ``invalid``
(see ``email_cleanup``), so a flaky network must never look like a dead domain.

A resolver that cannot reach a domain's nameservers says nothing about the
domain, so before giving up the lookup is retried on public fallback resolvers
(``EMAIL_VALIDATION_FALLBACK_DNS``, see ``_resolve``). That only ever turns an
``unknown`` into a real answer; it never turns an answer into something else.

What this cannot tell you: whether one specific *mailbox* exists. That needs an
SMTP ``RCPT TO`` probe, which is unreliable (greylisting, catch-all domains,
blocked port 25) and can damage the sending IP's reputation, so it is
deliberately not part of this module.
"""

import asyncio
import logging
import re
import time
from typing import Optional

import dns.asyncresolver
import dns.exception
import dns.resolver
import idna

from app.config import settings
from app.services.email_validation_data import (
    DISPOSABLE_DOMAINS,
    ROLE_LOCAL_PARTS,
    TOP_PROVIDER_DOMAINS,
    TYPO_DOMAINS,
    TYPO_TLDS,
)

logger = logging.getLogger("email_validation")

VERDICTS = ("valid", "risky", "unknown", "invalid")

# Reason codes. ``INVALID_REASONS`` are definitive and the only ones a cleanup
# may act on; the rest are informational.
REASON_SYNTAX = "syntax"
REASON_NO_DOMAIN = "no_domain"
REASON_NO_MX = "no_mx"
REASON_NULL_MX = "null_mx"
REASON_HARD_BOUNCE = "hard_bounce"
REASON_DISPOSABLE = "disposable"
REASON_ROLE = "role"
REASON_TYPO = "typo"
REASON_DNS_UNAVAILABLE = "dns_unavailable"

INVALID_REASONS = frozenset({
    REASON_SYNTAX, REASON_NO_DOMAIN, REASON_NO_MX, REASON_NULL_MX,
    REASON_HARD_BOUNCE,
})
# The subset decided by looking at the address itself (as opposed to ListMonk's
# bounce history). ``clean_invalid`` in the cleanup settings maps to these.
ADDRESS_INVALID_REASONS = frozenset({
    REASON_SYNTAX, REASON_NO_DOMAIN, REASON_NO_MX, REASON_NULL_MX,
})
RISK_REASONS = frozenset({REASON_DISPOSABLE, REASON_ROLE, REASON_TYPO})

MAX_ADDRESS_LEN = 254      # RFC 5321 §4.5.3.1.3 (path limit minus the angle brackets)
MAX_LOCAL_LEN = 64
MAX_DOMAIN_LEN = 253
MAX_LABEL_LEN = 63

# RFC 5322 "atext" plus the dot-atom rule (no leading/trailing/double dots).
# Non-ASCII letters are accepted (RFC 6531 / SMTPUTF8): rejecting them would
# let a cleanup blocklist perfectly deliverable internationalised addresses.
_LOCAL_ATOM = r"(?:[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]|[^\x00-\x7f])+"
_LOCAL_RE = re.compile(rf"^{_LOCAL_ATOM}(?:\.{_LOCAL_ATOM})*$")
_LABEL_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TLD_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")

# ── Domain result cache ─────────────────────────────────────────────────────
# A 100k-contact list usually spans a few thousand domains, so caching the DNS
# verdict per domain turns a scan from 100k lookups into a few thousand.

DOMAIN_TTL = 3600.0          # definitive answers
UNKNOWN_TTL = 60.0           # transient failures: retry soon, but don't hammer
MAX_CACHE_ENTRIES = 50_000

_domain_cache: dict[str, tuple[float, dict]] = {}
_inflight: dict[str, "asyncio.Task[dict]"] = {}
_dns_sem: Optional[asyncio.Semaphore] = None
_resolver: Optional[dns.asyncresolver.Resolver] = None
_fallback_resolvers: Optional[list] = None


def invalidate() -> None:
    """Drop every cached domain verdict and shared state (tests, admin reset).

    The semaphore and resolvers are dropped too: an ``asyncio.Semaphore`` binds
    to the first event loop that contends it, and a test run uses a new loop
    per test.
    """
    global _dns_sem, _resolver, _fallback_resolvers
    _domain_cache.clear()
    _inflight.clear()
    _dns_sem = None
    _resolver = None
    _fallback_resolvers = None


def _sweep_cache(now: float) -> None:
    """Keep the cache bounded: drop expired entries, then the oldest tenth."""
    if len(_domain_cache) < MAX_CACHE_ENTRIES:
        return
    for domain in [d for d, (exp, _) in _domain_cache.items() if exp <= now]:
        del _domain_cache[domain]
    if len(_domain_cache) >= MAX_CACHE_ENTRIES:
        oldest = sorted(_domain_cache.items(), key=lambda kv: kv[1][0])
        for domain, _ in oldest[: max(1, MAX_CACHE_ENTRIES // 10)]:
            del _domain_cache[domain]


# ── Syntax ──────────────────────────────────────────────────────────────────

def normalise(email: str) -> str:
    """Trim and lowercase, like ListMonk does when it stores an address."""
    return (email or "").strip().lower()


def _ascii_domain(domain: str) -> Optional[str]:
    """The punycode (A-label) form of ``domain``, or ``None`` if it has none."""
    if domain.isascii():
        return domain
    try:
        return idna.encode(domain, uts46=True).decode("ascii")
    except (idna.IDNAError, UnicodeError):
        return None


def parse_address(raw: str) -> tuple[bool, str, str, str]:
    """Check the shape of an address.

    Returns ``(ok, detail, local_part, ascii_domain)``. ``detail`` is a short
    human explanation when ``ok`` is false (shown in the UI).
    """
    email = normalise(raw)
    if not email:
        return False, "Empty address", "", ""
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in email):
        return False, "Contains spaces or control characters", "", ""
    if len(email) > MAX_ADDRESS_LEN:
        return False, f"Longer than {MAX_ADDRESS_LEN} characters", "", ""
    if email.count("@") != 1:
        return False, ("Missing the @ sign" if "@" not in email
                       else "More than one @ sign"), "", ""
    local, domain = email.split("@")
    if not local:
        return False, "Nothing before the @", "", ""
    if len(local) > MAX_LOCAL_LEN:
        return False, f"Part before the @ is longer than {MAX_LOCAL_LEN} characters", "", ""
    if not _LOCAL_RE.match(local):
        return False, "Part before the @ has invalid characters or misplaced dots", "", ""
    if not domain:
        return False, "Nothing after the @", local, ""
    if domain.startswith("[") or domain.endswith("]"):
        return False, "IP-address domains are not accepted", local, ""

    ascii_domain = _ascii_domain(domain)
    if ascii_domain is None:
        return False, "Domain is not a valid internationalised name", local, ""
    if len(ascii_domain) > MAX_DOMAIN_LEN:
        return False, "Domain is too long", local, ""
    labels = ascii_domain.split(".")
    if len(labels) < 2:
        return False, "Domain has no dot (e.g. example.com)", local, ""
    if any(not _LABEL_RE.match(label) for label in labels):
        return False, "Domain has invalid characters, empty parts or a bad hyphen", local, ""
    if not _TLD_RE.match(labels[-1]):
        return False, "Domain ending is not valid", local, ""
    return True, "", local, ascii_domain


# ── DNS ─────────────────────────────────────────────────────────────────────

def _get_resolver() -> dns.asyncresolver.Resolver:
    """Lazily build the shared resolver (reads /etc/resolv.conf)."""
    global _resolver
    if _resolver is None:
        timeout = float(settings.email_validation_dns_timeout)
        try:
            resolver = dns.asyncresolver.Resolver()
        except dns.resolver.NoResolverConfiguration:
            # Minimal containers can lack resolv.conf: fall back to public
            # resolvers rather than reporting every domain as unknown.
            resolver = dns.asyncresolver.Resolver(configure=False)
            resolver.nameservers = ["1.1.1.1", "8.8.8.8"]
        resolver.timeout = timeout
        resolver.lifetime = timeout
        _resolver = resolver
    return _resolver


def _get_fallback_resolvers() -> list:
    """Lazily build one resolver per configured fallback server, in order.

    Empty when ``EMAIL_VALIDATION_FALLBACK_DNS`` is empty (fallback disabled).
    """
    global _fallback_resolvers
    if _fallback_resolvers is None:
        timeout = float(settings.email_validation_dns_timeout)
        built = []
        for server in settings.email_validation_fallback_dns:
            resolver = dns.asyncresolver.Resolver(configure=False)
            resolver.nameservers = [server]
            resolver.timeout = timeout
            resolver.lifetime = timeout
            built.append(resolver)
        _fallback_resolvers = built
    return _fallback_resolvers


# An answer, even a negative one: the resolver reached an authority and was told.
_ANSWERS = (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer)


async def _resolve(domain: str, rdtype: str):
    """One DNS query. The single seam tests replace to avoid the network.

    Raises ``dns.resolver.NXDOMAIN`` (name does not exist), ``NoAnswer`` (name
    exists, no record of this type) or any other exception for everything
    transient.

    The system resolver is asked first. Only when it gives *no answer at all*
    (SERVFAIL, timeout, socket error) are the fallback resolvers tried, in
    order: a resolver that cannot reach a domain's nameservers says nothing
    about the domain, and another one often can. An answer, including NXDOMAIN
    or NoAnswer, is taken as it comes from whichever resolver gave it; if every
    resolver fails, the system resolver's error is raised, which the caller
    reports as ``unknown`` and never as ``invalid``.
    """
    try:
        return await _get_resolver().resolve(domain, rdtype, raise_on_no_answer=True)
    except _ANSWERS:
        raise
    except Exception as first:
        for resolver in _get_fallback_resolvers():
            try:
                answer = await resolver.resolve(domain, rdtype, raise_on_no_answer=True)
                logger.debug(f"DNS {rdtype} for {domain} answered by a fallback resolver "
                             f"after {type(first).__name__}")
                return answer
            except _ANSWERS:
                raise
            except Exception:
                continue
        raise first


def _get_sem() -> asyncio.Semaphore:
    global _dns_sem
    if _dns_sem is None:
        _dns_sem = asyncio.Semaphore(max(1, settings.email_validation_concurrency))
    return _dns_sem


def _result(status: str, reason: Optional[str] = None, detail: str = "") -> dict:
    return {"status": status, "reason": reason, "detail": detail}


async def _lookup(domain: str) -> dict:
    """Uncached DNS verdict for ``domain``.

    ``status`` is ``ok``, ``invalid`` (definitive) or ``unknown`` (transient).
    """
    async with _get_sem():
        try:
            answer = await _resolve(domain, "MX")
        except dns.resolver.NXDOMAIN:
            return _result("invalid", REASON_NO_DOMAIN, "This domain does not exist")
        except dns.resolver.NoAnswer:
            answer = None
        except dns.exception.DNSException as e:
            return _result("unknown", REASON_DNS_UNAVAILABLE,
                           f"DNS lookup failed ({type(e).__name__})")

        if answer is not None:
            targets = [str(r.exchange).rstrip(".") for r in answer]
            if any(targets):
                return _result("ok")
            # RFC 7505 "Null MX": the domain says it never accepts mail.
            return _result("invalid", REASON_NULL_MX,
                           "This domain publishes a Null MX (it accepts no email)")

        # No MX record: RFC 5321 §5.1 says deliver to the domain's own address.
        transient = False
        for rdtype in ("A", "AAAA"):
            try:
                await _resolve(domain, rdtype)
                return _result("ok")
            except dns.resolver.NXDOMAIN:
                return _result("invalid", REASON_NO_DOMAIN, "This domain does not exist")
            except dns.resolver.NoAnswer:
                continue
            except dns.exception.DNSException:
                transient = True
        if transient:
            return _result("unknown", REASON_DNS_UNAVAILABLE, "DNS lookup failed")
        return _result("invalid", REASON_NO_MX,
                       "This domain has no mail server (no MX or address record)")


async def check_domain(domain: str) -> dict:
    """Cached DNS verdict for ``domain`` (see ``_lookup``)."""
    now = time.monotonic()
    hit = _domain_cache.get(domain)
    if hit and hit[0] > now:
        return dict(hit[1])

    task = _inflight.get(domain)
    if task is None:
        async def run() -> dict:
            try:
                result = await _lookup(domain)
            except Exception as e:
                # Anything that is not a clean DNS answer (a raw OSError from
                # the socket layer, say) is transient by definition: it must
                # never be mistaken for a dead domain.
                logger.warning(f"DNS lookup for {domain} failed: {e!r}")
                result = _result("unknown", REASON_DNS_UNAVAILABLE,
                                 f"DNS lookup failed ({type(e).__name__})")
            finally:
                _inflight.pop(domain, None)
            ttl = UNKNOWN_TTL if result["status"] == "unknown" else DOMAIN_TTL
            _sweep_cache(time.monotonic())
            _domain_cache[domain] = (time.monotonic() + ttl, result)
            return result

        task = asyncio.ensure_future(run())
        _inflight[domain] = task
    # shield: one caller being cancelled must not cancel the lookup the other
    # callers are waiting on.
    return dict(await asyncio.shield(task))


HEALTH_PROBE_DOMAINS = ("gmail.com", "outlook.com")


async def resolver_healthy() -> bool:
    """True when DNS demonstrably works (a well-known domain resolves).

    Bypasses the cache: this gate protects destructive runs, so it must reflect
    the resolver *now*.
    """
    for domain in HEALTH_PROBE_DOMAINS:
        try:
            if (await _lookup(domain))["status"] == "ok":
                return True
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"Resolver health probe for {domain} failed: {e}")
    return False


# ── Risk flags ──────────────────────────────────────────────────────────────

def is_disposable(domain: str) -> bool:
    """True for a disposable domain or any subdomain of one."""
    labels = domain.split(".")
    return any(".".join(labels[i:]) in DISPOSABLE_DOMAINS
               for i in range(len(labels) - 1))


def is_role_account(local: str) -> bool:
    """True for ``support@``-style addresses (``support+tag@`` counts as ``support``).

    ``info@`` and ``hello@`` are not role accounts here: see ``ROLE_LOCAL_PARTS``.
    """
    return local.split("+", 1)[0] in ROLE_LOCAL_PARTS


def _is_typing_slip(a: str, b: str) -> bool:
    """True when ``a`` is ``b`` with one letter dropped, doubled or two swapped.

    Substitutions are deliberately excluded: that is exactly how two *different*
    real providers differ (``email.com`` vs ``gmail.com``), so treating them as
    typos would flag legitimate domains. Common substitution typos live in the
    explicit ``TYPO_DOMAINS`` table instead.
    """
    if a == b:
        return False
    if len(a) == len(b):
        diffs = [i for i in range(len(a)) if a[i] != b[i]]
        return (len(diffs) == 2 and diffs[1] == diffs[0] + 1
                and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]])
    if abs(len(a) - len(b)) != 1:
        return False
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(short) and short[i] == long_[i]:
        i += 1
    return short[i:] == long_[i + 1:]


# Only providers with a long enough name are matched by edit distance: with a
# short name ("msn", "aol") one slip reaches plenty of unrelated, real domains,
# and a false "did you mean" is worse than a missed one.
_FUZZY_PROVIDERS = tuple(d for d in TOP_PROVIDER_DOMAINS if len(d.split(".")[0]) >= 5)


def suggest_domain(domain: str) -> Optional[str]:
    """A likely intended domain when ``domain`` looks like a typo, else ``None``."""
    if domain in TOP_PROVIDER_DOMAINS:
        return None
    if domain in TYPO_DOMAINS:
        return TYPO_DOMAINS[domain]
    name, _, tld = domain.rpartition(".")
    if name and tld in TYPO_TLDS:
        return f"{name}.{TYPO_TLDS[tld]}"
    for provider in _FUZZY_PROVIDERS:
        if _is_typing_slip(domain, provider):
            return provider
    return None


# ── Public API ──────────────────────────────────────────────────────────────

async def validate_email(email: str) -> dict:
    """Run every check on one address.

    Returns ``{email, domain, verdict, reasons, suggestion, detail}``.
    ``reasons`` lists every finding (definitive reason first); ``detail`` is a
    human sentence about the most important one.
    """
    raw = email if isinstance(email, str) else ""
    normalised = normalise(raw)
    ok, detail, local, domain = parse_address(raw)
    out = {
        "email": normalised,
        "domain": domain,
        "verdict": "valid",
        "reasons": [],
        "suggestion": None,
        "detail": "",
    }
    if not ok:
        out.update(verdict="invalid", reasons=[REASON_SYNTAX], detail=detail)
        return out

    dns_result = await check_domain(domain)
    flags: list[str] = []
    if is_disposable(domain):
        flags.append(REASON_DISPOSABLE)
    if is_role_account(local):
        flags.append(REASON_ROLE)
    suggestion = suggest_domain(domain)
    if suggestion:
        flags.append(REASON_TYPO)
        out["suggestion"] = f"{local}@{suggestion}"

    if dns_result["status"] == "invalid":
        out.update(verdict="invalid", reasons=[dns_result["reason"], *flags],
                   detail=dns_result["detail"])
    elif flags:
        reasons = list(flags)
        if dns_result["status"] == "unknown":
            reasons.append(REASON_DNS_UNAVAILABLE)
        out.update(verdict="risky", reasons=reasons, detail=_flag_detail(flags[0], out))
    elif dns_result["status"] == "unknown":
        out.update(verdict="unknown", reasons=[REASON_DNS_UNAVAILABLE],
                   detail=dns_result["detail"])
    return out


def _flag_detail(flag: str, out: dict) -> str:
    if flag == REASON_DISPOSABLE:
        return "Disposable (throwaway) email provider"
    if flag == REASON_ROLE:
        return "Role account (shared inbox, not a person)"
    if flag == REASON_TYPO:
        return f"Possible typo, did you mean {out['suggestion']}?"
    return ""


async def validate_many(emails: list[str]) -> list[dict]:
    """Validate several addresses concurrently (DNS is bounded by the semaphore)."""
    return list(await asyncio.gather(*(validate_email(e) for e in emails)))
