"""Tests for domain blocklist (DNSBL/URIBL) lookups — DNS is mocked."""

import pytest

from app import domain_blocklist as dbl
from app.domain_blocklist import (
    LookupUnavailable,
    _interpret_spamhaus_dbl,
    _interpret_surbl,
    _interpret_uribl,
    check_domain,
    check_domains,
    normalise_domain,
)
from app.models import Inbox
from app.routers import inbox as inbox_router


@pytest.fixture(autouse=True)
def _clear_cache():
    dbl.clear_cache()
    yield
    dbl.clear_cache()


def make_resolver(answers: dict, calls: list | None = None):
    """answers maps qname -> list of codes, or an Exception instance to raise."""

    async def _resolve(qname: str):
        if calls is not None:
            calls.append(qname)
        value = answers.get(qname, [])
        if isinstance(value, Exception):
            raise value
        return value

    return _resolve


def test_normalise_domain():
    assert normalise_domain("Bob@Example.COM") == "example.com"
    assert normalise_domain("mail.example.com.") == "mail.example.com"
    assert normalise_domain("") == ""
    assert normalise_domain(None) == ""
    assert normalise_domain("localhost") == ""


def test_spamhaus_codes():
    r = _interpret_spamhaus_dbl(["127.0.1.2"])
    assert r.listed and r.meanings == ["spam domain"]
    r = _interpret_spamhaus_dbl(["127.0.1.4", "127.0.1.102"])
    assert r.listed and r.meanings == ["phishing domain", "abused legit spam"]
    r = _interpret_spamhaus_dbl(["127.255.255.254"])
    assert not r.listed and "public" in r.unavailable_reason
    r = _interpret_spamhaus_dbl(["127.255.255.255"])
    assert not r.listed and r.unavailable_reason


def test_surbl_bitmask():
    r = _interpret_surbl(["127.0.0.8"])
    assert r.listed and r.meanings == ["phishing"]
    r = _interpret_surbl(["127.0.0.80"])  # 16 + 64
    assert r.listed and set(r.meanings) == {"malware", "abuse / spam"}
    r = _interpret_surbl(["127.0.0.1"])
    assert not r.listed and r.unavailable_reason


def test_uribl_bitmask():
    r = _interpret_uribl(["127.0.0.2"])
    assert r.listed and r.meanings[0].startswith("black")
    r = _interpret_uribl(["127.0.0.12"])  # grey + red
    assert r.listed and len(r.meanings) == 2
    r = _interpret_uribl(["127.0.0.1"])
    assert not r.listed and "refused" in r.unavailable_reason


@pytest.mark.asyncio
async def test_check_domain_clean():
    result = await check_domain("example.com", make_resolver({}))
    assert result["listed"] is False
    assert result["listings"] == []
    assert result["unavailable"] == []


@pytest.mark.asyncio
async def test_check_domain_listed_and_unavailable():
    resolver = make_resolver(
        {
            "bad.com.dbl.spamhaus.org": ["127.0.1.2"],
            "bad.com.multi.surbl.org": LookupUnavailable("Timeout"),
            "bad.com.multi.uribl.com": ["127.0.0.1"],
        }
    )
    result = await check_domain("bad.com", resolver)
    assert result["listed"] is True
    assert [l["list"] for l in result["listings"]] == ["Spamhaus DBL"]
    assert result["listings"][0]["meanings"] == ["spam domain"]
    assert {u["list"] for u in result["unavailable"]} == {"SURBL", "URIBL"}


@pytest.mark.asyncio
async def test_refused_code_is_not_listed():
    resolver = make_resolver({"ok.com.dbl.spamhaus.org": ["127.255.255.254"]})
    result = await check_domain("ok.com", resolver)
    assert result["listed"] is False
    assert result["unavailable"][0]["list"] == "Spamhaus DBL"


@pytest.mark.asyncio
async def test_check_domains_dedupes_and_caches():
    calls: list[str] = []
    resolver = make_resolver({"bad.com.multi.uribl.com": ["127.0.0.2"]}, calls)
    out = await check_domains(
        ["a@bad.com", "b@BAD.com", "c@good.com", "", None], resolver=resolver
    )
    assert set(out) == {"bad.com", "good.com"}
    assert out["bad.com"]["listed"] is True
    assert out["good.com"]["listed"] is False
    assert len(calls) == 2 * len(dbl.BLOCKLISTS)

    await check_domains(["x@bad.com"], resolver=resolver)
    assert len(calls) == 2 * len(dbl.BLOCKLISTS)  # served from cache

    await check_domains(["x@bad.com"], refresh=True, resolver=resolver)
    assert len(calls) == 3 * len(dbl.BLOCKLISTS)


@pytest.mark.asyncio
async def test_endpoint_returns_domains(session, monkeypatch):
    session.add_all([Inbox(email="one@bad.com"), Inbox(email="two@bad.com"), Inbox(email="me@good.com")])
    await session.flush()

    calls: list[str] = []
    resolver = make_resolver({"bad.com.dbl.spamhaus.org": ["127.0.1.4"]}, calls)
    monkeypatch.setattr(dbl, "_dns_resolve", resolver)

    data = await inbox_router.inbox_domain_blocklist(refresh=False, db=session)
    assert set(data["domains"]) == {"bad.com", "good.com"}
    assert data["domains"]["bad.com"]["listings"][0]["meanings"] == ["phishing domain"]
    assert data["domains"]["good.com"]["listed"] is False
    assert len(calls) == 2 * len(dbl.BLOCKLISTS)
    assert sorted(data["inboxes"].values()) == ["bad.com", "bad.com", "good.com"]
