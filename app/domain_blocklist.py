"""Domain blocklist (DNSBL / URIBL) checks for sending domains.

Looks up ``<domain>.<zone>`` A records against a few major domain-based
blocklists. NXDOMAIN means "not listed". Some return codes mean the list
refused to answer (e.g. the query came through a public resolver) — those
are reported as "check unavailable", never as a listing.

Results are cached in memory per domain so the Inboxes page stays fast.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

log = logging.getLogger("quickly.domain_blocklist")

LOOKUP_TIMEOUT_SECONDS = 3.0
CACHE_TTL_SECONDS = 6 * 60 * 60
# Re-try sooner when a check could not be completed.
UNAVAILABLE_CACHE_TTL_SECONDS = 30 * 60


@dataclass
class Interpretation:
    listed: bool
    meanings: list[str] = field(default_factory=list)
    unavailable_reason: str = ""


def _interpret_spamhaus_dbl(codes: list[str]) -> Interpretation:
    meanings_map = {
        "127.0.1.2": "spam domain",
        "127.0.1.4": "phishing domain",
        "127.0.1.5": "malware domain",
        "127.0.1.6": "botnet C&C domain",
        "127.0.1.102": "abused legit spam",
        "127.0.1.103": "abused spammed redirector domain",
        "127.0.1.104": "abused legit phishing",
        "127.0.1.105": "abused legit malware",
        "127.0.1.106": "abused legit botnet C&C",
    }
    refused = {
        "127.255.255.252": "typing error in DNSBL name",
        "127.255.255.254": "query via public/open resolver refused",
        "127.255.255.255": "excessive number of queries",
    }
    meanings: list[str] = []
    for code in codes:
        if code in refused:
            return Interpretation(False, unavailable_reason=refused[code])
        if code.startswith("127.255.255."):
            return Interpretation(False, unavailable_reason=f"query refused ({code})")
        if code.startswith("127.0.1."):
            meanings.append(meanings_map.get(code, f"listed ({code})"))
    return Interpretation(bool(meanings), meanings)


def _interpret_surbl(codes: list[str]) -> Interpretation:
    bits = [
        (8, "phishing"),
        (16, "malware"),
        (64, "abuse / spam"),
        (128, "cracked site"),
        (2, "spam (legacy SC)"),
        (4, "spam (legacy WS)"),
        (32, "abuse (legacy AB)"),
    ]
    meanings: list[str] = []
    for code in codes:
        parts = code.split(".")
        if len(parts) != 4 or parts[0] != "127":
            continue
        try:
            last = int(parts[3])
        except ValueError:
            continue
        if last == 1:
            return Interpretation(False, unavailable_reason="query refused (blocked resolver)")
        for bit, label in bits:
            if last & bit and label not in meanings:
                meanings.append(label)
        if not meanings:
            meanings.append(f"listed ({code})")
    return Interpretation(bool(meanings), meanings)


def _interpret_uribl(codes: list[str]) -> Interpretation:
    bits = [
        (2, "black (actively used in spam)"),
        (4, "grey (bulk / mixed-reputation sender)"),
        (8, "red (newly observed in spam)"),
    ]
    meanings: list[str] = []
    for code in codes:
        parts = code.split(".")
        if len(parts) != 4 or parts[0] != "127":
            continue
        try:
            last = int(parts[3])
        except ValueError:
            continue
        if last & 1:
            return Interpretation(
                False, unavailable_reason="query refused (public resolver or rate limit)"
            )
        for bit, label in bits:
            if last & bit and label not in meanings:
                meanings.append(label)
    return Interpretation(bool(meanings), meanings)


@dataclass(frozen=True)
class Blocklist:
    name: str
    zone: str
    interpret: Callable[[list[str]], Interpretation]


BLOCKLISTS: tuple[Blocklist, ...] = (
    Blocklist("Spamhaus DBL", "dbl.spamhaus.org", _interpret_spamhaus_dbl),
    Blocklist("SURBL", "multi.surbl.org", _interpret_surbl),
    Blocklist("URIBL", "multi.uribl.com", _interpret_uribl),
)


class LookupUnavailable(Exception):
    """Raised by a resolver when the lookup could not complete (timeout etc.)."""


# A resolver takes a fully-qualified query name and returns the A-record
# addresses, ``[]`` for NXDOMAIN / no answer, or raises ``LookupUnavailable``.
Resolver = Callable[[str], "asyncio.Future[list[str]]"]


async def _dns_resolve(qname: str) -> list[str]:
    import dns.asyncresolver
    import dns.exception
    import dns.resolver

    try:
        answer = await dns.asyncresolver.resolve(qname, "A", lifetime=LOOKUP_TIMEOUT_SECONDS)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
        raise LookupUnavailable(type(exc).__name__) from exc
    except dns.exception.DNSException as exc:
        raise LookupUnavailable(type(exc).__name__) from exc
    return [r.to_text() for r in answer]


def normalise_domain(value: str | None) -> str:
    """Extract a bare lowercase domain from an email address or hostname."""
    raw = (value or "").strip().lower()
    if "@" in raw:
        raw = raw.rsplit("@", 1)[1]
    raw = raw.strip().strip(".")
    if not raw or "." not in raw or " " in raw:
        return ""
    return raw


async def check_domain(domain: str, resolver: Optional[Resolver] = None) -> dict:
    """Check a single domain against every list in ``BLOCKLISTS``."""
    resolve = resolver or _dns_resolve

    async def _one(bl: Blocklist) -> dict:
        entry = {"list": bl.name, "zone": bl.zone, "codes": [], "meanings": []}
        try:
            codes = await asyncio.wait_for(
                resolve(f"{domain}.{bl.zone}"), timeout=LOOKUP_TIMEOUT_SECONDS + 1
            )
        except (LookupUnavailable, asyncio.TimeoutError) as exc:
            entry["status"] = "unavailable"
            entry["reason"] = str(exc) or "lookup timed out"
            return entry
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("Blocklist lookup %s on %s failed: %s", domain, bl.zone, exc)
            entry["status"] = "unavailable"
            entry["reason"] = "lookup failed"
            return entry
        entry["codes"] = list(codes)
        if not codes:
            entry["status"] = "clean"
            return entry
        interp = bl.interpret(list(codes))
        if interp.unavailable_reason:
            entry["status"] = "unavailable"
            entry["reason"] = interp.unavailable_reason
        elif interp.listed:
            entry["status"] = "listed"
            entry["meanings"] = interp.meanings
        else:
            entry["status"] = "clean"
        return entry

    results = await asyncio.gather(*(_one(bl) for bl in BLOCKLISTS))
    listings = [
        {"list": r["list"], "zone": r["zone"], "codes": r["codes"], "meanings": r["meanings"]}
        for r in results
        if r["status"] == "listed"
    ]
    unavailable = [
        {"list": r["list"], "zone": r["zone"], "reason": r.get("reason", "")}
        for r in results
        if r["status"] == "unavailable"
    ]
    return {
        "domain": domain,
        "listed": bool(listings),
        "listings": listings,
        "unavailable": unavailable,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# domain -> (expires_at monotonic, result)
_cache: dict[str, tuple[float, dict]] = {}
_inflight: dict[str, asyncio.Task] = {}


def clear_cache() -> None:
    _cache.clear()
    _inflight.clear()


async def check_domains(
    domains: Iterable[str],
    *,
    refresh: bool = False,
    resolver: Optional[Resolver] = None,
) -> dict[str, dict]:
    """Check each unique domain once (concurrently), using a TTL cache."""
    unique = sorted({d for d in (normalise_domain(x) for x in domains) if d})
    now = time.monotonic()
    out: dict[str, dict] = {}
    to_fetch: list[str] = []
    for d in unique:
        cached = _cache.get(d)
        if cached and not refresh and cached[0] > now:
            out[d] = cached[1]
        else:
            to_fetch.append(d)

    async def _fetch(d: str) -> dict:
        task = _inflight.get(d)
        if task is None:
            task = asyncio.ensure_future(check_domain(d, resolver))
            _inflight[d] = task
        try:
            result = await task
        finally:
            _inflight.pop(d, None)
        ttl = (
            UNAVAILABLE_CACHE_TTL_SECONDS
            if result["unavailable"] and not result["listed"]
            else CACHE_TTL_SECONDS
        )
        _cache[d] = (time.monotonic() + ttl, result)
        return result

    if to_fetch:
        fetched = await asyncio.gather(*(_fetch(d) for d in to_fetch))
        out.update(zip(to_fetch, fetched))
    return out
