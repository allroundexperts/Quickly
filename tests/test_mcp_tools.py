"""MCP tools proxy to the matching REST endpoints."""

import json

import httpx
import pytest

from app import mcp_leads


@pytest.fixture
def captured(monkeypatch):
    """Route MCP outbound HTTP to a mock transport and record each request."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/leads/export"):
            return httpx.Response(200, text="email,name\na@example.com,A\n")
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(mcp_leads.httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(mcp_leads, "_outbound_headers", lambda ctx: {"X-API-Key": "k"})
    monkeypatch.setattr(mcp_leads, "_api_base", lambda: "http://quickly.test")
    return requests


async def _call(name: str, args: dict):
    return await mcp_leads.leads_mcp.call_tool(name, args)


def _body(request: httpx.Request):
    return json.loads(request.content)


async def test_campaign_tools_registered():
    names = {t.name for t in await mcp_leads.leads_mcp.list_tools()}
    expected = {
        "list_campaigns", "get_campaign", "create_campaign", "update_campaign",
        "delete_campaign", "duplicate_campaign", "reorder_campaigns",
        "list_sequence_steps", "create_sequence_step", "update_sequence_step",
        "delete_sequence_step", "list_sequence_variants", "create_sequence_variant",
        "update_sequence_variant", "delete_sequence_variant",
        "preview_campaign_email", "send_test_email",
        "list_campaign_leads", "add_campaign_leads", "import_campaign_leads_csv",
        "export_campaign_leads_csv", "update_campaign_lead", "remove_campaign_lead",
        "write_custom_email", "reconcile_personalized", "verify_campaign_leads",
        "get_campaign_verification_status", "detect_campaign_lead_providers",
        "list_campaign_queue", "list_campaign_sent_emails", "get_campaign_step_analytics",
        "campaigns_have_leads",
    }
    assert expected <= names


async def test_create_campaign_sends_only_provided_fields(captured):
    await _call("create_campaign", {"name": "Q4", "inbox_ids": [2, 1], "track_opens": True})
    (req,) = captured
    assert req.method == "POST"
    assert req.url.path == "/api/campaigns"
    assert req.headers["x-api-key"] == "k"
    assert _body(req) == {"name": "Q4", "inbox_ids": [2, 1], "track_opens": True}


async def test_update_campaign_settings_patch(captured):
    await _call("update_campaign", {"campaign_id": 7, "paused": False, "sending_days": [0, 2]})
    (req,) = captured
    assert req.method == "PATCH"
    assert req.url.path == "/api/campaigns/7"
    assert _body(req) == {"paused": False, "sending_days": [0, 2]}


async def test_update_campaign_requires_a_field(captured):
    await _call("update_campaign", {"campaign_id": 7})
    assert captured == []


async def test_create_and_update_sequence_step(captured):
    await _call("create_sequence_step", {"campaign_id": 3, "position": 0, "subject": "Hi", "body": "Hello"})
    await _call("update_sequence_step", {"campaign_id": 3, "sequence_id": 9, "wait_days_after_previous": 2})
    create, update = captured
    assert (create.method, create.url.path) == ("POST", "/api/campaigns/3/sequences")
    assert _body(create) == {
        "position": 0,
        "subject": "Hi",
        "body": "Hello",
        "wait_days_after_previous": 0,
        "sequence_type": "standard",
    }
    assert (update.method, update.url.path) == ("PATCH", "/api/campaigns/3/sequences/9")
    assert _body(update) == {"wait_days_after_previous": 2}


async def test_variant_paths(captured):
    await _call("create_sequence_variant", {"campaign_id": 1, "sequence_id": 2, "body": "B"})
    await _call("delete_sequence_variant", {"campaign_id": 1, "sequence_id": 2, "variant_id": 5})
    create, delete = captured
    assert create.url.path == "/api/campaigns/1/sequences/2/variants"
    assert _body(create) == {"label": "", "body": "B", "enabled": True}
    assert (delete.method, delete.url.path) == ("DELETE", "/api/campaigns/1/sequences/2/variants/5")


async def test_import_csv_uploads_multipart(captured):
    await _call("import_campaign_leads_csv", {"campaign_id": 4, "csv_content": "email\nx@example.com\n"})
    (req,) = captured
    assert req.url.path == "/api/campaigns/4/leads/import"
    assert req.url.params["skip_duplicates"] == "true"
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b"x@example.com" in req.content


async def test_export_csv_returns_text(captured):
    result = await _call("export_campaign_leads_csv", {"campaign_id": 4, "status": "active"})
    (req,) = captured
    assert req.url.params["status"] == "active"
    assert "verification_status" not in req.url.params
    assert "a@example.com" in json.dumps(result, default=str)
