"""Email verification provider layer.

Supports pluggable providers. Currently implements MailtesterNinja.
To add a new provider, subclass ``BaseVerificationProvider`` and register it
in ``PROVIDERS``.
"""
import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger("quickly.email_verification")

# ── Result status constants ──────────────────────────────────────────────────
VALID = "valid"
INVALID = "invalid"
CATCH_ALL = "catch_all"
UNKNOWN = "unknown"
RISKY = "risky"
PENDING = "pending"

# Statuses that should block sending
BLOCK_SEND_STATUSES = {INVALID, RISKY}


class VerificationResult:
    """Normalised output returned by every provider."""

    def __init__(
        self,
        email: str,
        status: str,
        raw: dict | None = None,
        message: str = "",
        error: bool = False,
    ):
        self.email = email
        self.status = status        # one of the constants above
        self.raw = raw or {}        # raw provider response for debugging
        self.message = message      # human-readable explanation
        # True when the provider could not be reached or answered with an
        # error (timeout, HTTP error, bad JSON). Such results are retryable,
        # unlike a genuine "unknown" verdict from the provider.
        self.error = error


# ── Base class ───────────────────────────────────────────────────────────────
class BaseVerificationProvider(ABC):
    """Interface that every email-verification backend must implement."""

    @abstractmethod
    async def verify(self, email: str, api_key: str) -> VerificationResult:
        """Verify a single email address. Must not raise on transient errors."""
        ...


# ── MailtesterNinja provider ─────────────────────────────────────────────────
class MailtesterNinjaProvider(BaseVerificationProvider):
    """https://mailtester.ninja – single-email verification endpoint."""

    BASE_URL = "https://happy.mailtester.ninja/ninja"

    async def verify(self, email: str, api_key: str) -> VerificationResult:
        url = f"{self.BASE_URL}?key={api_key}&email={email}"
        async with httpx.AsyncClient() as client:
            for attempt in range(3):
                try:
                    resp = await client.get(url, timeout=15)
                    if resp.status_code == 429:
                        await asyncio.sleep(3 * (attempt + 1))
                        continue
                    if resp.status_code != 200:
                        return VerificationResult(email, UNKNOWN, message=f"HTTP {resp.status_code}", error=True)
                    data = resp.json()
                    return self._normalise(email, data)
                except (httpx.RequestError, Exception) as exc:
                    log.warning("MailtesterNinja attempt %d failed for %s: %s", attempt + 1, email, exc)
                    await asyncio.sleep(2)
        return VerificationResult(email, UNKNOWN, message="All retries exhausted", error=True)

    @staticmethod
    def _normalise(email: str, data: dict) -> VerificationResult:
        code = data.get("code", "")
        message = data.get("message", "")

        if message == "Catch-All":
            return VerificationResult(email, CATCH_ALL, raw=data, message="Catch-all domain")

        if code == "ok" or message in ("Accepted", "Limited"):
            return VerificationResult(email, VALID, raw=data, message=message or "Valid")

        # Risky indicators
        if message in ("Low Quality", "Low Deliverability"):
            return VerificationResult(email, RISKY, raw=data, message=message)

        # Everything else is invalid
        return VerificationResult(email, INVALID, raw=data, message=message or "Invalid")


# ── Custom HTTP provider ──────────────────────────────────────────────────────

_MISSING = object()


def _path_parts(path: str) -> list[str]:
    """Split a field path into segments.  Accepts ``a.b.c``, list indexes as
    ``items.0.status`` or ``items[0].status``, and an optional ``$.`` prefix."""
    path = (path or "").strip()
    if path.startswith("$"):
        path = path[1:].lstrip(".")
    path = re.sub(r"\[(\d+)\]", r".\1", path)
    return [p for p in path.split(".") if p != ""]


def _lookup(data: Any, path: str) -> Any:
    """Like :func:`_get_nested` but returns ``_MISSING`` when the path does not
    exist (so a present ``null`` can be told apart from a missing field)."""
    value: Any = data
    for part in _path_parts(path):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.lstrip("-").isdigit() and -len(value) <= int(part) < len(value):
            value = value[int(part)]
        else:
            return _MISSING
    return value


def _get_nested(data: Any, path: str) -> Any:
    """Traverse *data* following *path* (see :func:`_path_parts`).  Returns
    None if any segment is missing."""
    value = _lookup(data, path)
    return None if value is _MISSING else value


def custom_config_error(url_template: str, method: str = "GET", body_template: str = "") -> str | None:
    """Validate a custom provider config. ``{email}`` must appear in the URL,
    or (for POST requests) in the request body."""
    url_template = (url_template or "").strip()
    if not url_template:
        return "URL is required"
    if not url_template.lower().startswith(("http://", "https://")):
        return "URL must start with http:// or https://"
    in_url = "{email}" in url_template
    in_body = (method or "GET").upper() == "POST" and "{email}" in (body_template or "")
    if not (in_url or in_body):
        return "{email} must appear in the URL" + (" or the request body" if (method or "").upper() == "POST" else "")
    return None


class CustomHttpProvider(BaseVerificationProvider):
    """User-configured HTTP/S verification endpoint.

    The caller supplies a URL template (and, for POST, an optional body
    template) containing ``{email}`` which is replaced with the address to
    verify.  Optional custom headers are sent with every request; header
    values may also contain ``{email}``.  A JSON body is sent as
    ``application/json`` unless a Content-Type header is configured.  A dot-notation *field_path* is
    used to extract a single value from the JSON response; that value is
    then matched against *valid_values* or *invalid_values* (case-insensitive)
    to produce the final status.  Any value not found in either list yields
    ``UNKNOWN``.
    """

    def __init__(
        self,
        url_template: str,
        field_path: str,
        valid_values: list[str],
        invalid_values: list[str],
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body_template: str = "",
    ) -> None:
        self.url_template = url_template.strip()
        self.field_path = field_path.strip()
        self.valid_values: set[str] = {v.strip().lower() for v in valid_values if v.strip()}
        self.invalid_values: set[str] = {v.strip().lower() for v in invalid_values if v.strip()}
        self.method = method.upper()
        self.headers: dict[str, str] = {
            str(k).strip(): str(v)
            for k, v in (headers or {}).items()
            if str(k).strip()
        }
        # Request body is only sent for POST requests
        self.body_template = (body_template or "") if self.method == "POST" else ""

    @property
    def config_error(self) -> str | None:
        """Return a human-readable error if the configuration is unusable."""
        return custom_config_error(self.url_template, self.method, self.body_template)

    def _build_body(self, email: str) -> tuple[str | None, bool]:
        """Render the body template. Returns (content, is_json)."""
        if not self.body_template.strip():
            return None, False
        try:
            # Substitute a JSON-escaped email so quotes/backslashes can't break JSON
            json_email = json.dumps(email)[1:-1]
            rendered = self.body_template.replace("{email}", json_email)
            json.loads(rendered)
            return rendered, True
        except ValueError:
            return self.body_template.replace("{email}", email), False

    async def verify(self, email: str, api_key: str) -> VerificationResult:  # noqa: ARG002
        err = self.config_error
        if err:
            return VerificationResult(email, UNKNOWN, message=err, error=True)
        url = self.url_template.replace("{email}", quote(email, safe="@"))
        headers = {k: v.replace("{email}", email) for k, v in self.headers.items()}
        content, is_json = self._build_body(email) if self.method == "POST" else (None, False)
        if content is not None and not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "application/json" if is_json else "text/plain"
        async with httpx.AsyncClient(follow_redirects=True) as client:
            try:
                if self.method == "POST":
                    resp = await client.post(url, headers=headers, content=content, timeout=15)
                else:
                    resp = await client.get(url, headers=headers, timeout=15)
                if resp.status_code != 200:
                    return VerificationResult(
                        email, UNKNOWN,
                        raw={"http_status": resp.status_code, "body": resp.text[:2000]},
                        message=f"HTTP {resp.status_code}", error=True,
                    )
                try:
                    data = resp.json()
                except Exception:
                    return VerificationResult(
                        email, UNKNOWN,
                        raw={"http_status": resp.status_code, "body": resp.text[:2000]},
                        message="Response was not valid JSON", error=True,
                    )
                return self._normalise(email, data)
            except httpx.RequestError as exc:
                return VerificationResult(email, UNKNOWN, message=f"Request error: {exc}", error=True)
            except Exception as exc:
                log.warning("CustomHttpProvider error for %s: %s", email, exc)
                return VerificationResult(email, UNKNOWN, message=f"Unexpected error: {exc}", error=True)

    def _normalise(self, email: str, data: Any) -> VerificationResult:
        raw_value = _get_nested(data, self.field_path) if self.field_path else None
        str_val = str(raw_value).strip().lower() if raw_value is not None else ""
        # A value listed as both good and bad counts as bad (the safer choice)
        if str_val in self.invalid_values:
            status = INVALID
        elif str_val in self.valid_values:
            status = VALID
        else:
            status = UNKNOWN
        message = f"field '{self.field_path}' = {raw_value!r}"
        return VerificationResult(email, status, raw=data, message=message)


# ── Provider registry ────────────────────────────────────────────────────────
PROVIDERS: dict[str, BaseVerificationProvider] = {
    "mailtester_ninja": MailtesterNinjaProvider(),
}

DEFAULT_PROVIDER = "mailtester_ninja"
ALL_PROVIDER_NAMES = ["mailtester_ninja", "custom"]


def get_provider(name: str | None = None) -> BaseVerificationProvider:
    """Return the provider instance for *name*, falling back to the default.

    Note: ``"custom"`` is not in the registry since it requires runtime
    configuration — use :func:`build_custom_provider` for that case.
    """
    return PROVIDERS.get(name or DEFAULT_PROVIDER, PROVIDERS[DEFAULT_PROVIDER])


# ── High-level helpers ───────────────────────────────────────────────────────

async def verify_single(email: str, api_key: str, provider_name: str | None = None) -> VerificationResult:
    """Verify a single email using the specified (or default) provider."""
    provider = get_provider(provider_name)
    return await provider.verify(email, api_key)


async def verify_batch(
    emails: list[str],
    api_key: str,
    provider_name: str | None = None,
    concurrency: int = 5,
    delay: float = 1.0,
) -> list[VerificationResult]:
    """Verify a list of emails with bounded concurrency."""
    provider = get_provider(provider_name)
    sem = asyncio.Semaphore(concurrency)
    results: list[VerificationResult] = []

    async def _verify_one(em: str) -> VerificationResult:
        async with sem:
            result = await provider.verify(em, api_key)
            await asyncio.sleep(delay)
            return result

    tasks = [_verify_one(em) for em in emails]
    results = await asyncio.gather(*tasks)
    return list(results)
