"""Custom HTTP verification provider: field paths, value mapping and the
settings "Run test" diagnostics."""

import httpx
import pytest

import app.email_verification as ev
from app.email_verification import INVALID, UNKNOWN, VALID, CustomHttpProvider, _MISSING, _lookup


RESPONSE = {
    "status": "ok",
    "data": {"result": "risky", "flags": None},
    "results": [{"status": "deliverable"}, {"status": "invalid"}],
}


@pytest.mark.parametrize(
    "path, expected",
    [
        ("status", "ok"),
        ("data.result", "risky"),
        ("$.data.result", "risky"),
        ("results.0.status", "deliverable"),
        ("results[1].status", "invalid"),
        ("results.-1.status", "invalid"),
        ("data.flags", None),
    ],
)
def test_lookup_paths(path, expected):
    assert _lookup(RESPONSE, path) == expected


@pytest.mark.parametrize("path", ["missing", "data.result.deep", "results.5.status", "results.x"])
def test_lookup_missing(path):
    assert _lookup(RESPONSE, path) is _MISSING


def test_value_in_both_lists_counts_as_invalid():
    provider = CustomHttpProvider(
        "https://v.test/?e={email}", "data.result",
        ["deliverable", "risky", "valid", "ok"], ["invalid", "blocked", "risky"],
    )
    assert provider._normalise("a@b.c", RESPONSE).status == INVALID
    assert provider._normalise("a@b.c", {"data": {"result": "Deliverable "}}).status == VALID
    assert provider._normalise("a@b.c", {"data": {"result": "maybe"}}).status == UNKNOWN


@pytest.fixture
def mock_api(monkeypatch):
    """Route the provider's HTTP calls to a handler keyed on the email."""
    def handler(request: httpx.Request) -> httpx.Response:
        email = request.url.params.get("email", "")
        if email.startswith("denied"):
            return httpx.Response(401, json={"error": "invalid api key"})
        if email.startswith("html"):
            return httpx.Response(200, text="<html>oops</html>")
        return httpx.Response(200, json={"data": {"status": "risky" if email.startswith("risky") else "ok"}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        ev.httpx, "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )


@pytest.mark.asyncio
async def test_run_test_endpoint_explains_each_row(session, mock_api):
    from app.routers.settings import _CustomProviderTestRequest, test_custom_email_verification

    payload = _CustomProviderTestRequest(
        url_template="https://v.test/check?email={email}",
        field_path="data.status",
        valid_values=["ok"],
        invalid_values=["risky"],
        test_emails=["good@x.test", "risky@x.test", "denied@x.test", "html@x.test"],
    )
    res = await test_custom_email_verification(payload, db=session)
    rows = {r["email"]: r for r in res["results"]}

    assert (rows["good@x.test"]["status"], rows["good@x.test"]["raw_field_value"]) == ("valid", "ok")
    assert rows["good@x.test"]["field_found"] is True
    assert rows["risky@x.test"]["status"] == "invalid"

    denied = rows["denied@x.test"]
    assert denied["error"] is True and denied["message"] == "HTTP 401"
    assert denied["raw_response"] == {"http_status": 401, "body": '{"error":"invalid api key"}'}

    html = rows["html@x.test"]
    assert html["error"] is True and "not valid JSON" in html["message"]
    assert html["raw_response"]["body"] == "<html>oops</html>"


@pytest.mark.asyncio
async def test_run_test_endpoint_flags_missing_field(session, mock_api):
    from app.routers.settings import _CustomProviderTestRequest, test_custom_email_verification

    payload = _CustomProviderTestRequest(
        url_template="https://v.test/check?email={email}",
        field_path="status",  # actual value lives at data.status
        valid_values=["ok"],
        test_emails=["good@x.test"],
    )
    (row,) = (await test_custom_email_verification(payload, db=session))["results"]
    assert row["error"] is False
    assert row["field_found"] is False
    assert row["raw_field_value"] is None
    assert row["status"] == "unknown"
    assert row["raw_response"] == {"data": {"status": "ok"}}
