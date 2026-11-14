"""Sending reputation: is the infrastructure your mail leaves from trusted?

The deliverability audit used to look only at ListMonk's own settings, so a
100 % score sat next to a sending IP whose reverse DNS pointed at another
domain (Yahoo: 550 5.7.25) and an IP on the Barracuda blocklist. This module
checks what receiving servers check, from public DNS:

* **Sending IPs** — ``SENDING_IPS`` when set, otherwise the addresses of
  ListMonk's enabled SMTP hosts (right when that server delivers the mail).
  A known email service (SES, SendGrid …) manages its own IPs: those checks
  are then informational.
* **Reverse DNS** — every IP has a PTR whose name resolves back to the IP
  (forward-confirmed), ideally a name inside the From domain.
* **Blocklists** — a handful of DNSBLs that real receivers use. Every list is
  first asked for its RFC 5782 test entry (127.0.0.2): a list that does not
  answer it (Spamhaus refuses queries from public resolvers) is reported as
  *unknown*, never as *clean*.
* **SPF / DKIM / DMARC** of the From domain: SPF is evaluated for the sending
  IP (ip4/ip6/a/mx/include/redirect/all, 10-lookup limit; macros unsupported),
  DKIM is looked for under common selectors (``DKIM_SELECTORS``), DMARC must
  exist (Gmail/Yahoo bulk-sender rule).
* **Rejections** — soft bounces of the last ``REJECTION_DAYS`` days that the
  bounce classifier tagged as a sender problem (spam/reputation policy,
  blocklist, reverse DNS, authentication), per mailbox provider.

Privacy: the blocklist operators see your *sending IPs* (that is how a DNSBL
works); no subscriber data leaves the server for these checks.

The report is cached for ``CACHE_SECONDS`` (``invalidate()``, reset by
tests/conftest.py); ``refresh=True`` re-checks.
"""

import asyncio
import ipaddress
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Optional

import dns.exception
import dns.resolver
import dns.reversename

from app.config import settings
from app.services import email_validation, send_stats

logger = logging.getLogger("sending_reputation")

CACHE_SECONDS = 15 * 60
REJECTION_DAYS = 7
REJECTION_MAX_PAGES = 5
REJECTION_PER_PAGE = 100
# Share of the period's sends rejected for spam/reputation that fails the check.
REJECTION_FAIL_RATE = 0.05
REJECTION_FAIL_MIN = 3
SPF_LOOKUP_LIMIT = 10

# (name, zone, where to request removal / look the IP up)
DNSBLS = [
    ("Spamhaus ZEN", "zen.spamhaus.org", "https://check.spamhaus.org/"),
    ("Barracuda", "b.barracudacentral.org", "https://www.barracudacentral.org/rbl/removal-request"),
    ("SpamCop", "bl.spamcop.net", "https://www.spamcop.net/bl.shtml"),
    ("Mailspike", "bl.mailspike.net", "https://mailspike.io/ip_verify"),
    ("PSBL", "psbl.surriel.com", "https://psbl.org/remove"),
]

# Bounce classifier tags (bounce_ingest.classify_bounce) that blame the sender.
SENDER_REASONS = {
    "soft: reputation_policy": "spam / reputation",
    "soft: blacklist": "blocklist",
    "soft: reverse_dns": "reverse DNS",
    "soft: auth_failed": "SPF / DKIM / DMARC",
}
# Always a configuration problem: one is enough to fail.
CONFIG_REASONS = {"soft: blacklist", "soft: reverse_dns", "soft: auth_failed"}

PROVIDERS = [
    ("Gmail", ("gmail.com", "googlemail.com")),
    ("Yahoo / AOL", ("yahoo.", "ymail.com", "rocketmail.com", "aol.com", "aim.com")),
    ("Microsoft", ("outlook.", "hotmail.", "live.", "msn.com", "passport.com")),
    ("Apple", ("icloud.com", "me.com", "mac.com")),
]

# Email services that run (and answer for) their own sending IPs.
ESP_HOST_SUFFIXES = {
    "amazonaws.com": "Amazon SES", "sendgrid.net": "SendGrid", "mailgun.org": "Mailgun",
    "postmarkapp.com": "Postmark", "sparkpostmail.com": "SparkPost", "brevo.com": "Brevo",
    "sendinblue.com": "Brevo", "mailjet.com": "Mailjet", "gmail.com": "Google",
    "googlemail.com": "Google", "office365.com": "Microsoft 365", "outlook.com": "Microsoft 365",
    "zoho.com": "Zoho", "zoho.eu": "Zoho", "smtp2go.com": "SMTP2GO", "mandrillapp.com": "Mandrill",
    "resend.com": "Resend", "elasticemail.com": "Elastic Email", "mailersend.net": "MailerSend",
}

_cache: dict = {"at": 0.0, "key": None, "report": None}


def invalidate() -> None:
    """Drop the cached report (tests, rule 11)."""
    _cache.update(at=0.0, key=None, report=None)


# ── DNS ─────────────────────────────────────────────────────────────────────

async def _query(name: str, rdtype: str) -> list[str]:
    """One DNS query as plain strings; the seam tests replace.

    ``[]`` for NXDOMAIN / no records of this type (an answer). Anything
    transient raises.
    """
    try:
        answer = await email_validation._resolve(name, rdtype)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    out = []
    for rr in answer:
        if rdtype == "TXT":
            out.append(b"".join(rr.strings).decode("utf-8", "replace"))
        elif rdtype in ("PTR", "CNAME"):
            out.append(rr.target.to_text().rstrip(".").lower())
        elif rdtype == "MX":
            out.append(rr.exchange.to_text().rstrip(".").lower())
        else:
            out.append(rr.to_text())
    return out


async def _try(name: str, rdtype: str) -> tuple[list[str], bool]:
    """``(records, ok)``: ok is False when the lookup itself failed."""
    try:
        return await _query(name, rdtype), True
    except (dns.exception.DNSException, OSError, asyncio.TimeoutError) as e:
        logger.debug(f"DNS {rdtype} {name} failed: {type(e).__name__}")
        return [], False
    except Exception as e:
        logger.warning(f"DNS {rdtype} {name} failed: {e}")
        return [], False


# ── Sending IPs ─────────────────────────────────────────────────────────────

def from_domain(raw_settings: dict) -> Optional[str]:
    addr = parseaddr(str(raw_settings.get("app.from_email") or ""))[1]
    if "@" not in addr:
        return None
    return addr.rsplit("@", 1)[1].strip().lower().rstrip(".") or None


def _provider_for(host: str) -> Optional[str]:
    host = host.lower().rstrip(".")
    for suffix, name in ESP_HOST_SUFFIXES.items():
        if host == suffix or host.endswith("." + suffix):
            return name
    return None


def _public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_global


async def find_sending_ips(raw_settings: dict) -> dict:
    """The IPs receivers see, and where that answer came from."""
    if settings.sending_ips:
        return {"ips": [ip for ip in settings.sending_ips if _public(ip)],
                "source": "env", "hosts": [], "provider": None, "note": None}
    hosts = []
    for block in raw_settings.get("smtp") or []:
        if isinstance(block, dict) and block.get("enabled") and block.get("host"):
            hosts.append(str(block["host"]).strip().lower())
    hosts = sorted(set(hosts))
    providers = {p for p in (_provider_for(h) for h in hosts) if p}
    ips: list[str] = []
    private = False
    for host in hosts:
        try:
            ipaddress.ip_address(host)
            found = [host]
        except ValueError:
            found, _ = await _try(host, "A")
        for ip in found:
            if _public(ip):
                if ip not in ips:
                    ips.append(ip)
            else:
                private = True
    note = None
    if not hosts:
        note = "No enabled SMTP server in ListMonk."
    elif not ips:
        note = ("The SMTP host is not a public address (a local relay?). "
                "Set SENDING_IPS to the IPs your mail leaves from." if private
                else "The SMTP host did not resolve. Set SENDING_IPS to check your IPs.")
    return {"ips": ips, "source": "smtp", "hosts": hosts,
            "provider": ", ".join(sorted(providers)) or None, "note": note}


# ── Reverse DNS ─────────────────────────────────────────────────────────────

def _in_domain(host: Optional[str], domain: Optional[str]) -> bool:
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


async def check_reverse_dns(ip: str, domain: Optional[str]) -> dict:
    ptrs, ok = await _try(dns.reversename.from_address(ip).to_text(), "PTR")
    if not ok:
        return {"ip": ip, "ptr": None, "status": "unknown", "aligned": None}
    if not ptrs:
        return {"ip": ip, "ptr": None, "status": "missing", "aligned": False}
    ptr = ptrs[0]
    rdtype = "AAAA" if ipaddress.ip_address(ip).version == 6 else "A"
    forward, ok = await _try(ptr, rdtype)
    if not ok:
        status = "unknown"
    else:
        canonical = {str(ipaddress.ip_address(a)) for a in forward if _is_ip(a)}
        status = "ok" if str(ipaddress.ip_address(ip)) in canonical else "mismatch"
    return {"ip": ip, "ptr": ptr, "status": status, "forward": forward,
            "aligned": _in_domain(ptr, domain)}


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


# ── Blocklists ──────────────────────────────────────────────────────────────

def _listing_codes(records: list[str]) -> tuple[list[str], bool]:
    """``(codes, error)``: 127.255.255.x answers are the operator refusing."""
    codes, error = [], False
    for rec in records:
        if not _is_ip(rec):
            continue
        addr = ipaddress.ip_address(rec)
        if addr in ipaddress.ip_network("127.255.255.0/24"):
            error = True
        elif addr in ipaddress.ip_network("127.0.0.0/8"):
            codes.append(rec)
    return codes, error


async def _list_works(zone: str) -> bool:
    """RFC 5782: a working DNSBL lists 127.0.0.2."""
    records, ok = await _try(f"2.0.0.127.{zone}", "A")
    codes, error = _listing_codes(records)
    return ok and bool(codes) and not error


async def check_blocklists(ips: list[str]) -> dict:
    zones = [z for _, z, _ in DNSBLS]
    works = dict(zip(zones, await asyncio.gather(*(_list_works(z) for z in zones))))

    async def one(ip: str, name: str, zone: str, url: str) -> dict:
        row = {"ip": ip, "list": name, "zone": zone, "url": url}
        if not works[zone]:
            return {**row, "status": "unknown", "codes": []}
        rev = ".".join(reversed(ip.split(".")))
        records, ok = await _try(f"{rev}.{zone}", "A")
        codes, error = _listing_codes(records)
        if not ok or error:
            return {**row, "status": "unknown", "codes": []}
        return {**row, "status": "listed" if codes else "clean", "codes": codes}

    v4 = [ip for ip in ips if ipaddress.ip_address(ip).version == 4]
    results = await asyncio.gather(*(one(ip, n, z, u) for ip in v4 for n, z, u in DNSBLS))
    return {
        "lists": [{"name": n, "zone": z, "available": works[z]} for n, z, _ in DNSBLS],
        "results": list(results),
    }


# ── SPF ─────────────────────────────────────────────────────────────────────

class _SpfError(Exception):
    def __init__(self, result: str, detail: str):
        super().__init__(detail)
        self.result = result
        self.detail = detail


def _spf_record(records: list[str]) -> Optional[str]:
    spf = [r for r in records if re.match(r"^v=spf1(\s|$)", r.strip(), re.I)]
    if len(spf) > 1:
        raise _SpfError("permerror", "more than one SPF record")
    return spf[0].strip() if spf else None


def _net_match(ip: str, addresses: list[str], cidr4: int, cidr6: int) -> bool:
    target = ipaddress.ip_address(ip)
    for a in addresses:
        if not _is_ip(a):
            continue
        addr = ipaddress.ip_address(a)
        if addr.version != target.version:
            continue
        prefix = cidr4 if addr.version == 4 else cidr6
        if target in ipaddress.ip_network(f"{addr}/{prefix}", strict=False):
            return True
    return False


async def _spf_eval(domain: str, ip: str, counter: list[int], depth: int = 0) -> tuple[str, str]:
    if depth > SPF_LOOKUP_LIMIT:
        raise _SpfError("permerror", "include loop")
    records, ok = await _try(domain, "TXT")
    if not ok:
        raise _SpfError("temperror", f"TXT lookup for {domain} failed")
    record = _spf_record(records)
    if record is None:
        return "none", f"no SPF record on {domain}"

    def lookup() -> None:
        counter[0] += 1
        if counter[0] > SPF_LOOKUP_LIMIT:
            raise _SpfError("permerror", "more than 10 DNS lookups")

    rdtype = "AAAA" if ipaddress.ip_address(ip).version == 6 else "A"
    redirect = None
    qualifiers = {"+": "pass", "-": "fail", "~": "softfail", "?": "neutral"}
    for term in record.split()[1:]:
        if term.lower().startswith("redirect="):
            redirect = term.split("=", 1)[1]
            continue
        if "=" in term and ":" not in term.split("=", 1)[0]:
            continue  # other modifiers (exp=)
        qual = term[0] if term[0] in qualifiers else "+"
        mech = term[1:] if term[0] in qualifiers else term
        name, _, arg = mech.partition(":")
        name = name.lower()
        cidr4, cidr6 = 32, 128
        # a / mx carry optional dual CIDR lengths: a, a:dom, a/24, mx:dom/24//64
        parsed = re.match(r"^(a|mx)(?::([^/]+))?(?:/(\d+))?(?://(\d+))?$", mech, re.I)
        if parsed:
            name = parsed.group(1).lower()
            arg = parsed.group(2) or ""
            cidr4 = int(parsed.group(3) or 32)
            cidr6 = int(parsed.group(4) or 128)
        if "%" in arg:
            continue  # macros are not supported: skip the term
        matched = False
        if name == "all":
            matched = True
        elif name in ("ip4", "ip6"):
            try:
                matched = ipaddress.ip_address(ip) in ipaddress.ip_network(arg, strict=False)
            except ValueError:
                raise _SpfError("permerror", f"bad {name}:{arg}")
        elif name == "a":
            lookup()
            addrs, ok = await _try(arg or domain, rdtype)
            if not ok:
                raise _SpfError("temperror", f"lookup of {arg or domain} failed")
            matched = _net_match(ip, addrs, cidr4, cidr6)
        elif name == "mx":
            lookup()
            hosts, ok = await _try(arg or domain, "MX")
            if not ok:
                raise _SpfError("temperror", f"MX lookup of {arg or domain} failed")
            for host in hosts[:10]:
                addrs, ok = await _try(host, rdtype)
                if ok and _net_match(ip, addrs, cidr4, cidr6):
                    matched = True
                    break
        elif name == "include":
            lookup()
            result, _ = await _spf_eval(arg, ip, counter, depth + 1)
            if result == "none":
                raise _SpfError("permerror", f"include:{arg} has no SPF record")
            matched = result == "pass"
        elif name == "exists":
            lookup()
            addrs, ok = await _try(arg, "A")
            matched = ok and bool(addrs)
        elif name == "ptr":
            continue  # deprecated (RFC 7208 §5.5); receivers rarely honour it
        else:
            raise _SpfError("permerror", f"unknown mechanism {name}")
        if matched:
            return qualifiers[qual], f"{term} on {domain}"
    if redirect:
        lookup()
        return await _spf_eval(redirect, ip, counter, depth + 1)
    return "neutral", f"no mechanism of {domain} matched"


async def check_spf(domain: str, ips: list[str]) -> dict:
    records, ok = await _try(domain, "TXT")
    try:
        record = _spf_record(records) if ok else None
    except _SpfError as e:
        return {"record": None, "found": True, "error": False, "results": [],
                "status": "permerror", "detail": e.detail}
    out = {"record": record, "found": record is not None, "error": not ok, "results": []}
    for ip in ips:
        try:
            result, detail = await _spf_eval(domain, ip, [0])
        except _SpfError as e:
            result, detail = e.result, e.detail
        out["results"].append({"ip": ip, "result": result, "detail": detail})
    return out


# ── DKIM / DMARC ────────────────────────────────────────────────────────────

async def check_dkim(domain: str) -> dict:
    selectors = list(settings.dkim_selectors)
    looked = await asyncio.gather(*(_try(f"{s}._domainkey.{domain}", "TXT") for s in selectors))
    found, errors = [], 0
    for sel, (records, ok) in zip(selectors, looked):
        if not ok:
            errors += 1
        elif any(re.search(r"(^|;)\s*p=[A-Za-z0-9+/=]{20,}", r) for r in records):
            found.append(sel)
    return {"found": found, "checked": selectors, "error": errors == len(selectors) and bool(selectors)}


# Second-level labels that are a public suffix under a two-letter country code
# (co.uk, com.au, org.nz, ac.jp, ne.jp …). Without a bundled Public Suffix
# List this covers the registries that actually hand out such names; taking
# the last two labels made the organisational domain of mail.acme.co.uk
# "co.uk", so its DMARC record was looked up in the wrong place.
_CC_SECOND_LEVEL = {
    "co", "com", "net", "org", "gov", "edu", "ac", "ltd", "plc", "nhs", "sch",
    "ne", "or", "go", "gr", "ed", "lg", "id", "nom", "gen", "biz", "info", "mil",
}


def _org_domain(domain: str) -> str:
    parts = domain.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _CC_SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) > 2 else domain


async def check_dmarc(domain: str) -> dict:
    for name in dict.fromkeys([domain, _org_domain(domain)]):
        records, ok = await _try(f"_dmarc.{name}", "TXT")
        if not ok:
            return {"found": False, "error": True, "record": None, "policy": None}
        rec = next((r for r in records if r.strip().lower().startswith("v=dmarc1")), None)
        if rec:
            policy = re.search(r"(?:^|;)\s*p\s*=\s*(\w+)", rec, re.I)
            return {"found": True, "error": False, "record": rec,
                    "policy": policy.group(1).lower() if policy else None, "domain": name}
    return {"found": False, "error": False, "record": None, "policy": None}


# ── Rejections ──────────────────────────────────────────────────────────────

def provider_of(email: str) -> str:
    domain = email.rsplit("@", 1)[-1].lower()
    for name, needles in PROVIDERS:
        if any(domain.startswith(n) if n.endswith(".") else domain == n for n in needles):
            return name
    return "Other"


def _parse_time(value) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def check_rejections(listmonk, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=REJECTION_DAYS)
    rows = []
    for page in range(1, REJECTION_MAX_PAGES + 1):
        res = await listmonk.get_bounces(page=page, per_page=REJECTION_PER_PAGE, bounce_type="soft")
        data = res.get("data") if isinstance(res, dict) else None
        results = data.get("results") if isinstance(data, dict) else None
        results = [b for b in results if isinstance(b, dict)] if isinstance(results, list) else []
        older = False
        for b in results:
            created = _parse_time(b.get("created_at"))
            if created and created < since:
                older = True
                continue
            reason = ((b.get("meta") or {}).get("classify_reason")) or ""
            if reason in SENDER_REASONS:
                rows.append({
                    "email": b.get("email") or "",
                    "reason": reason,
                    "label": SENDER_REASONS[reason],
                    "code": (b.get("meta") or {}).get("smtp_code"),
                    "provider": provider_of(b.get("email") or ""),
                    "campaign": (b.get("campaign") or {}).get("name"),
                    "created_at": b.get("created_at"),
                })
        if older or len(results) < REJECTION_PER_PAGE:
            break
    by_reason: dict[str, int] = {}
    by_provider: dict[str, int] = {}
    for r in rows:
        by_reason[r["label"]] = by_reason.get(r["label"], 0) + 1
        by_provider[r["provider"]] = by_provider.get(r["provider"], 0) + 1
    try:
        sent = send_stats.daily(REJECTION_DAYS)["period_total"]
    except Exception:
        sent = None
    return {
        "days": REJECTION_DAYS,
        "total": len(rows),
        "config_total": sum(1 for r in rows if r["reason"] in CONFIG_REASONS),
        "by_reason": by_reason,
        "by_provider": by_provider,
        "sent": sent,
        "recent": rows[:10],
        "error": None,
    }


# ── Report ──────────────────────────────────────────────────────────────────

async def get_report(listmonk, raw_settings: Optional[dict] = None, refresh: bool = False) -> dict:
    """Every check above, cached for CACHE_SECONDS."""
    if raw_settings is None:
        raw_settings = ((await listmonk.get_settings()) or {}).get("data") or {}
    domain = from_domain(raw_settings)
    sending = await find_sending_ips(raw_settings)
    key = (domain, tuple(sending["ips"]), sending["provider"])
    if not refresh and _cache["report"] is not None and _cache["key"] == key \
            and time.monotonic() - _cache["at"] < CACHE_SECONDS:
        return _cache["report"]

    ips = sending["ips"]

    async def rejections() -> dict:
        try:
            return await check_rejections(listmonk)
        except Exception as e:
            logger.warning(f"Sending reputation: bounce history unavailable: {e}")
            return {"days": REJECTION_DAYS, "total": 0, "config_total": 0, "by_reason": {},
                    "by_provider": {}, "sent": None, "recent": [], "error": str(e)}

    async def nothing() -> None:
        return None

    rdns, lists, spf, dkim, dmarc, rej = await asyncio.gather(
        asyncio.gather(*(check_reverse_dns(ip, domain) for ip in ips)),
        check_blocklists(ips),
        check_spf(domain, ips) if domain else nothing(),
        check_dkim(domain) if domain else nothing(),
        check_dmarc(domain) if domain else nothing(),
        rejections(),
    )
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "from_domain": domain,
        "sending": sending,
        "reverse_dns": list(rdns),
        "blocklists": lists,
        "spf": spf,
        "dkim": dkim,
        "dmarc": dmarc,
        "rejections": rej,
    }
    _cache.update(at=time.monotonic(), key=key, report=report)
    return report


# ── Audit checks ────────────────────────────────────────────────────────────

def _check(cid, name, description, status, value, recommended, impact, chips=None, links=None,
           steps=None):
    return {"id": cid, "group": "reputation", "name": name, "description": description,
            "status": status, "current_value": value, "recommended": recommended,
            "impact": impact, "chips": chips or [], "links": links or [], "steps": steps or []}


# Where each big mailbox provider shows a sender its own reputation.
PROVIDER_TOOLS = {
    "Gmail": ("Google Postmaster Tools (domain & IP reputation)", "https://postmaster.google.com/"),
    "Microsoft": ("Microsoft SNDS (IP reputation)", "https://sendersupport.olc.protection.outlook.com/snds/"),
    "Yahoo / AOL": ("Yahoo Sender Hub", "https://senders.yahooinc.com/"),
}


def _rejection_steps(rej: dict, domain: Optional[str]) -> tuple[list[str], list[dict]]:
    """What to do about sender-blamed rejections, by reason and provider."""
    reasons = rej.get("by_reason") or {}
    providers = [p for p in sorted(rej.get("by_provider") or {}) if p != "Other"]
    steps: list[str] = []
    if reasons.get(SENDER_REASONS["soft: blacklist"]):
        steps.append("Blocklist: find the listing in the \"Sending IPs Not on Blocklists\" row and "
                     "request removal before sending more.")
    if reasons.get(SENDER_REASONS["soft: reverse_dns"]):
        steps.append("Reverse DNS: set the sending IP's PTR at the VPS provider to a name that "
                     "resolves back to the IP (see the Reverse DNS row).")
    if reasons.get(SENDER_REASONS["soft: auth_failed"]):
        steps.append("Authentication: send a test to an inbox you own and open the raw message — "
                     "SPF, DKIM and DMARC must all say pass"
                     + (f", with DKIM d= on {domain}." if domain else "."))
    if reasons.get(SENDER_REASONS["soft: reputation_policy"]):
        who = ", ".join(providers) if providers else "the rejecting receivers"
        steps += [
            f"Spam / reputation: hold back the {who} addresses for now; every further "
            "rejection lowers the domain's standing.",
            "Lower the daily volume (Settings → Warm-up) and send to contacts who open and "
            "reply before going back to cold addresses.",
            "Keep the mail plain and personal: plain text, one link at most, no images or tracking.",
        ]
    links = [{"label": PROVIDER_TOOLS[p][0], "url": PROVIDER_TOOLS[p][1]}
             for p in providers if p in PROVIDER_TOOLS]
    return steps, links


def build_checks(report: dict) -> list[dict]:
    """The report as deliverability-audit rows (group ``reputation``)."""
    sending = report["sending"]
    ips = sending["ips"]
    managed = sending.get("provider") and sending.get("source") != "env"
    domain = report.get("from_domain")
    checks = []
    no_ip = sending.get("note") or "No sending IP found."

    # Reverse DNS (forward-confirmed).
    rows = report["reverse_dns"]
    bad = [r for r in rows if r["status"] in ("missing", "mismatch")]
    unknown = [r for r in rows if r["status"] == "unknown"]
    chips = []
    for r in rows:
        if r["status"] == "ok":
            chips.append({"label": f"{r['ip']} → {r['ptr']}"})
        elif r["status"] == "missing":
            chips.append({"label": f"{r['ip']}: no reverse DNS", "tone": "bad"})
        elif r["status"] == "mismatch":
            chips.append({"label": f"{r['ip']} → {r['ptr']} (points elsewhere)", "tone": "bad", "wide": True})
        else:
            chips.append({"label": f"{r['ip']}: lookup failed", "tone": "warn"})
    checks.append(_check(
        "reverse_dns", "Reverse DNS of Sending IPs",
        "Receivers look up the name of the IP that connects (PTR) and check that this name points "
        "back to the same IP. Without it Yahoo rejects outright (550 5.7.25) and others score the mail "
        "as spam. Set it with whoever owns the IP (the VPS provider's panel).",
        "info" if not ips or managed or (unknown and not bad) else ("fail" if bad else "pass"),
        f"managed by {sending['provider']}" if managed else (", ".join(c["label"] for c in chips) or no_ip),
        "PTR name that resolves back to the IP", "Critical", chips,
    ))

    # PTR inside the From domain.
    misaligned = [r for r in rows if r.get("ptr") and r["status"] == "ok" and not r.get("aligned")]
    checks.append(_check(
        "ptr_alignment", "Reverse DNS Matches the From Domain",
        "The IP's name should belong to the domain you send as (mail.yourdomain.com). A name from "
        "another domain is legal but looks like shared or borrowed infrastructure to filters.",
        "info" if not ips or managed or not domain or not any(r.get("ptr") for r in rows)
        else ("warn" if misaligned else "pass"),
        ", ".join(f"{r['ip']} → {r['ptr']}" for r in misaligned) or (f"inside {domain}" if domain else no_ip),
        f"a name under {domain}" if domain else "a name under the From domain", "Medium",
        [{"label": f"{r['ptr']} is not under {domain}", "tone": "warn", "wide": True} for r in misaligned],
    ))

    # Blocklists.
    bl = report["blocklists"]
    listed = [r for r in bl["results"] if r["status"] == "listed"]
    available = [x for x in bl["lists"] if x["available"]]
    unavailable = [x["name"] for x in bl["lists"] if not x["available"]]
    chips = [{"label": f"{r['ip']} on {r['list']}", "tone": "bad"} for r in listed]
    if ips:
        chips += [{"label": f"{name}: not checkable here", "tone": "warn"} for name in unavailable]
    links = [{"label": f"Request removal from {r['list']} ({r['ip']})", "url": r["url"]} for r in listed]
    if "Spamhaus ZEN" in unavailable and ips:
        links.append({"label": "Check Spamhaus by hand", "url": "https://check.spamhaus.org/"})
    checks.append(_check(
        "ip_blocklists", "Sending IPs Not on Blocklists",
        "Many receivers refuse mail from IPs on public blocklists (DNSBLs). A list that does not "
        "answer its own test entry from this server (Spamhaus refuses public resolvers) is reported "
        "as not checkable, never as clean.",
        "info" if not ips or managed or not available else ("fail" if listed else "pass"),
        (", ".join(c["label"] for c in chips if c.get("tone") == "bad")
         or (f"clean on {len(available)} of {len(bl['lists'])} lists" if ips and available else no_ip)),
        "listed nowhere", "Critical", chips, links,
    ))

    # SPF.
    spf = report.get("spf")
    if not domain or spf is None:
        status, value = "info", "no From address in ListMonk"
    elif spf.get("error"):
        status, value = "info", "SPF lookup failed"
    elif not spf.get("found"):
        status, value = "fail", f"no SPF record on {domain}"
    elif spf.get("status") == "permerror":
        status, value = "fail", f"broken SPF: {spf.get('detail')}"
    elif not spf["results"]:
        status, value = "pass", spf["record"]
    else:
        results = {r["result"] for r in spf["results"]}
        if results <= {"pass"}:
            status = "pass"
        elif results & {"fail", "softfail", "none", "permerror"}:
            status = "fail"
        elif results & {"temperror"}:
            status = "info"
        else:
            status = "warn"
        value = ", ".join(f"{r['ip']}: {r['result']}" for r in spf["results"])
    checks.append(_check(
        "spf", "SPF Authorises the Sending IP",
        "The From domain's SPF record must include every IP the mail leaves from; Gmail and Yahoo "
        "require SPF or DKIM to pass, and DMARC alignment on top.",
        status, value, "pass for every sending IP", "Critical",
        [{"label": f"{r['ip']}: {r['result']}", "tone": "bad" if r["result"] != "pass" else None}
         for r in (spf or {}).get("results", [])],
    ))

    # DKIM.
    dkim = report.get("dkim")
    if not domain or dkim is None:
        status, value = "info", "no From address in ListMonk"
    elif dkim["found"]:
        status, value = "pass", "selector " + ", ".join(dkim["found"])
    elif dkim["error"]:
        status, value = "info", "DKIM lookup failed"
    else:
        status, value = "warn", "no key under " + ", ".join(dkim["checked"])
    checks.append(_check(
        "dkim", "DKIM Key Published",
        "A DKIM signature proves the mail was not altered and ties it to your domain. The selector "
        "cannot be read from DNS, so common ones are tried; set DKIM_SELECTORS if yours differs.",
        status, value, "a key for the selector your mail server signs with", "High",
    ))

    # DMARC.
    dmarc = report.get("dmarc")
    if not domain or dmarc is None:
        status, value = "info", "no From address in ListMonk"
    elif dmarc["error"]:
        status, value = "info", "DMARC lookup failed"
    elif dmarc["found"]:
        status, value = "pass", f"p={dmarc.get('policy') or '?'}"
    else:
        status, value = "fail", f"no DMARC record on {domain}"
    checks.append(_check(
        "dmarc", "DMARC Record Published",
        "Gmail and Yahoo require a DMARC record from bulk senders (p=none is enough to start).",
        status, value, "v=DMARC1; p=none (or stricter)", "Critical",
    ))

    # Rejections over the last days.
    rej = report["rejections"]
    steps, links = [], []
    if rej.get("error"):
        status, value = "info", "bounce history unavailable"
    else:
        rate = (rej["total"] / rej["sent"]) if rej.get("sent") else None
        if rej["config_total"]:
            status = "fail"
        elif rej["total"] >= REJECTION_FAIL_MIN and rate is not None and rate >= REJECTION_FAIL_RATE:
            status = "fail"
        elif rej["total"]:
            status = "warn"
        else:
            status = "pass"
        value = (f"{rej['total']} in {rej['days']} days"
                 + (f" ({rate:.0%} of {rej['sent']} sent)" if rate is not None else "")) if rej["total"] \
            else f"none in {rej['days']} days"
        if rej["total"]:
            steps, links = _rejection_steps(rej, domain)
    # The count (and its share of the period's sends) leads: it is what the status is judged on.
    chips = [{"label": value, "wide": True}] if rej.get("total") and not rej.get("error") else []
    chips += [{"label": f"{p}: {n}", "tone": "bad"} for p, n in sorted(rej.get("by_provider", {}).items())]
    chips += [{"label": f"{label}: {n}", "tone": "warn"} for label, n in sorted(rej.get("by_reason", {}).items())]
    checks.append(_check(
        "sender_rejections", "Receivers Accepting Your Mail",
        f"Bounces of the last {rej['days']} days where the receiver blamed the sender, not the "
        "address: flagged as spam or low reputation, a blocklist, reverse DNS or failed "
        "authentication. They count as soft bounces, but they mean the mail never arrived.",
        status, value, "0 rejections", "Critical", chips, links, steps,
    ))
    return checks


def alerts(report: dict) -> list[dict]:
    """Problems worth a dashboard alert: ``[{"tone", "text"}]``."""
    out = []
    for r in report["blocklists"]["results"]:
        if r["status"] == "listed":
            out.append({"tone": "danger", "text": f"Sending IP {r['ip']} is on the {r['list']} blocklist"})
    if not (report["sending"].get("provider") and report["sending"].get("source") != "env"):
        for r in report["reverse_dns"]:
            if r["status"] == "missing":
                out.append({"tone": "danger", "text": f"Sending IP {r['ip']} has no reverse DNS"})
            elif r["status"] == "mismatch":
                out.append({"tone": "danger",
                            "text": f"Reverse DNS of {r['ip']} ({r['ptr']}) does not point back to it"})
    rej = report["rejections"]
    if not rej.get("error") and rej["total"]:
        parts = ", ".join(f"{p} {n}" for p, n in sorted(rej["by_provider"].items()))
        out.append({"tone": "warning" if not rej["config_total"] else "danger",
                    "text": f"{rej['total']} email{'s' if rej['total'] != 1 else ''} rejected as a sender "
                            f"problem in {rej['days']} days ({parts})"})
    return out
