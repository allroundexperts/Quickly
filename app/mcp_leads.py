"""Remote MCP (Streamable HTTP) for Quickly leads and campaigns — mounted at ``/api/mcp``."""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.database import AsyncSessionLocal
from app.auth import try_resolve_user_for_mcp
from app.settings_manager import settings as app_settings

log = logging.getLogger("quickly.mcp_leads")

leads_mcp = FastMCP(
    "quickly-leads",
    instructions=(
        "Quickly leads and campaigns API tools. Authenticate MCP HTTP requests with X-API-Key "
        "(Settings → API Keys) or Authorization: Bearer (JWT)."
    ),
    # Default FastMCP host is 127.0.0.1, which enables MCP DNS-rebinding checks with
    # localhost-only allowed Host headers — breaks real Hosts (e.g. quickly.example.com)
    # behind Caddy. Disable here; Quickly already terminates TLS and validates access.
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    stateless_http=True,
    streamable_http_path="/",
)

# Force session manager creation (used by StreamableHTTPASGIApp and app lifespan).
leads_mcp.streamable_http_app()
_mcp_http_handler = StreamableHTTPASGIApp(leads_mcp.session_manager)


def _api_base() -> str:
    return (app_settings.base_url or "http://localhost:8000").rstrip("/")


def _outbound_headers(ctx: Context) -> dict[str, str]:
    rc = ctx.request_context
    req = rc.request
    if not isinstance(req, Request):
        raise RuntimeError("MCP tools require an HTTP request context")
    h: dict[str, str] = {"Accept": "application/json"}
    ak = req.headers.get("x-api-key")
    auth = req.headers.get("authorization")
    if ak:
        h["X-API-Key"] = ak
    elif auth:
        h["Authorization"] = auth
    else:
        raise RuntimeError("Missing X-API-Key or Authorization on MCP request")
    return h


def _json_response(r: httpx.Response) -> str:
    try:
        data = r.json()
    except Exception:
        data = {"status_code": r.status_code, "text": r.text[:8000]}
    if r.is_error:
        data = {"error": True, "status_code": r.status_code, "detail": data}
    return json.dumps(data, indent=2, default=str)


def _compact(**fields: Any) -> dict[str, Any]:
    """Drop ``None`` values so PATCH bodies only carry fields the caller set."""
    return {k: v for k, v in fields.items() if v is not None}


def _bool_param(value: bool) -> str:
    return "true" if value else "false"


async def _request(
    ctx: Context,
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    json_body: Any = None,
    files: dict[str, Any] | None = None,
    timeout: float = 120.0,
) -> httpx.Response:
    headers = _outbound_headers(ctx)
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.request(
            method,
            f"{_api_base()}{path}",
            headers=headers,
            params=params,
            json=json_body,
            files=files,
        )


async def _call(ctx: Context, method: str, path: str, **kwargs: Any) -> str:
    return _json_response(await _request(ctx, method, path, **kwargs))


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


@leads_mcp.tool()
async def list_leads(
    ctx: Context,
    q: str = "",
    status: str = "",
    bad_only: bool = False,
    interest: str = "",
) -> str:
    """List leads with optional filters (search, enrollment status, bad_only, interest); filters stack with AND."""
    params: dict[str, str] = {}
    if q.strip():
        params["q"] = q.strip()
    if status.strip():
        params["status"] = status.strip()
    if bad_only:
        params["bad_only"] = "true"
    if interest.strip():
        params["interest"] = interest.strip()
    return await _call(ctx, "GET", "/api/leads", params=params)


@leads_mcp.tool()
async def get_lead(ctx: Context, lead_id: int) -> str:
    """Get one lead by id, including campaign enrollments."""
    return await _call(ctx, "GET", f"/api/leads/{lead_id}", timeout=60.0)


@leads_mcp.tool()
async def update_lead(
    ctx: Context,
    lead_id: int,
    name: str | None = None,
    enrollment_status: str | None = None,
    custom_data: dict[str, Any] | None = None,
) -> str:
    """Patch lead fields (name, enrollment_status on all campaigns, custom_data). Enrollment changes recalculate the queue."""
    body = _compact(name=name, enrollment_status=enrollment_status, custom_data=custom_data)
    if not body:
        return json.dumps({"error": "Provide at least one of: name, enrollment_status, custom_data"})
    return await _call(ctx, "PATCH", f"/api/leads/{lead_id}", json_body=body)


@leads_mcp.tool()
async def delete_lead(ctx: Context, lead_id: int) -> str:
    """Delete a lead and associated logs; recalculates queue if enrolled."""
    return await _call(ctx, "DELETE", f"/api/leads/{lead_id}")


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


@leads_mcp.tool()
async def list_campaigns(ctx: Context) -> str:
    """List all campaigns with settings, inbox ids and stats."""
    return await _call(ctx, "GET", "/api/campaigns")


@leads_mcp.tool()
async def get_campaign(ctx: Context, campaign_id: int) -> str:
    """Get one campaign by id, including settings, inbox ids and stats."""
    return await _call(ctx, "GET", f"/api/campaigns/{campaign_id}")


@leads_mcp.tool()
async def campaigns_have_leads(ctx: Context) -> str:
    """Return whether any campaign has enrolled leads (``{"has_leads": bool}``)."""
    return await _call(ctx, "GET", "/api/campaigns/has-leads")


@leads_mcp.tool()
async def create_campaign(
    ctx: Context,
    name: str,
    inbox_ids: list[int],
    sending_days: list[int] | None = None,
    sending_hours_start: str | None = None,
    sending_hours_end: str | None = None,
    stop_on_reply: bool | None = None,
    paused: bool | None = None,
    priority: int | None = None,
    track_opens: bool | None = None,
    track_clicks: bool | None = None,
    add_unsubscribe_header: bool | None = None,
    add_one_click_unsubscribe: bool | None = None,
    send_first_as_text: bool | None = None,
    send_all_as_text: bool | None = None,
    timezone: str | None = None,
    match_lead_provider: bool | None = None,
    custom_sequence_mode: str | None = None,
) -> str:
    """Create a campaign. inbox_ids order = inbox priority. sending_days uses Mon=0..Sun=6; hours are "HH:MM";
    timezone is an IANA name; custom_sequence_mode is wait_for_all | asap. Omitted settings use server defaults.
    Add steps afterwards with create_sequence_step."""
    body = _compact(
        name=name,
        inbox_ids=inbox_ids,
        sending_days=sending_days,
        sending_hours_start=sending_hours_start,
        sending_hours_end=sending_hours_end,
        stop_on_reply=stop_on_reply,
        paused=paused,
        priority=priority,
        track_opens=track_opens,
        track_clicks=track_clicks,
        add_unsubscribe_header=add_unsubscribe_header,
        add_one_click_unsubscribe=add_one_click_unsubscribe,
        send_first_as_text=send_first_as_text,
        send_all_as_text=send_all_as_text,
        timezone=timezone,
        match_lead_provider=match_lead_provider,
        custom_sequence_mode=custom_sequence_mode,
    )
    return await _call(ctx, "POST", "/api/campaigns", json_body=body)


@leads_mcp.tool()
async def update_campaign(
    ctx: Context,
    campaign_id: int,
    name: str | None = None,
    inbox_ids: list[int] | None = None,
    sending_days: list[int] | None = None,
    sending_hours_start: str | None = None,
    sending_hours_end: str | None = None,
    stop_on_reply: bool | None = None,
    paused: bool | None = None,
    priority: int | None = None,
    track_opens: bool | None = None,
    track_clicks: bool | None = None,
    add_unsubscribe_header: bool | None = None,
    add_one_click_unsubscribe: bool | None = None,
    send_first_as_text: bool | None = None,
    send_all_as_text: bool | None = None,
    timezone: str | None = None,
    match_lead_provider: bool | None = None,
    custom_sequence_mode: str | None = None,
) -> str:
    """Update campaign settings (partial). Only provided fields change; inbox_ids replaces the full inbox list.
    Use paused=true/false to pause or resume sending. Schedule-affecting changes recalculate the queue."""
    body = _compact(
        name=name,
        inbox_ids=inbox_ids,
        sending_days=sending_days,
        sending_hours_start=sending_hours_start,
        sending_hours_end=sending_hours_end,
        stop_on_reply=stop_on_reply,
        paused=paused,
        priority=priority,
        track_opens=track_opens,
        track_clicks=track_clicks,
        add_unsubscribe_header=add_unsubscribe_header,
        add_one_click_unsubscribe=add_one_click_unsubscribe,
        send_first_as_text=send_first_as_text,
        send_all_as_text=send_all_as_text,
        timezone=timezone,
        match_lead_provider=match_lead_provider,
        custom_sequence_mode=custom_sequence_mode,
    )
    if not body:
        return json.dumps({"error": "Provide at least one campaign field to update"})
    return await _call(ctx, "PATCH", f"/api/campaigns/{campaign_id}", json_body=body)


@leads_mcp.tool()
async def delete_campaign(ctx: Context, campaign_id: int) -> str:
    """Delete a campaign with its sequences, enrollments and queued sends. Leads enrolled only in this
    campaign are deleted too."""
    return await _call(ctx, "DELETE", f"/api/campaigns/{campaign_id}")


@leads_mcp.tool()
async def duplicate_campaign(ctx: Context, campaign_id: int) -> str:
    """Copy a campaign with all its sequence steps, without enrolled leads."""
    return await _call(ctx, "POST", f"/api/campaigns/{campaign_id}/duplicate")


@leads_mcp.tool()
async def reorder_campaigns(ctx: Context, campaign_ids: list[int]) -> str:
    """Set campaign priority order (highest priority first) used by priority-based scheduling."""
    return await _call(ctx, "POST", "/api/campaigns/reorder", json_body={"campaign_ids": campaign_ids})


# ---------------------------------------------------------------------------
# Sequence steps and A/B variants
# ---------------------------------------------------------------------------


@leads_mcp.tool()
async def list_sequence_steps(ctx: Context, campaign_id: int) -> str:
    """List a campaign's sequence steps (ordered by position), including A/B variants."""
    return await _call(ctx, "GET", f"/api/campaigns/{campaign_id}/sequences")


@leads_mcp.tool()
async def create_sequence_step(
    ctx: Context,
    campaign_id: int,
    position: int,
    body: str,
    subject: str | None = None,
    wait_days_after_previous: int = 0,
    is_html: bool | None = None,
    preview_text: str | None = None,
    sequence_type: str = "standard",
    fallback_subject: str | None = None,
    fallback_body: str | None = None,
) -> str:
    """Add a sequence step. position is 0-based (0 = first email).
    sequence_type is standard | personalized (per-lead custom emails; fallback_* used when none is written).
    is_html: true = HTML, false = plain text, omitted = auto-detect. Body/subject support {{variables}}."""
    payload = _compact(
        position=position,
        subject=subject,
        body=body,
        wait_days_after_previous=wait_days_after_previous,
        is_html=is_html,
        preview_text=preview_text,
        sequence_type=sequence_type,
        fallback_subject=fallback_subject,
        fallback_body=fallback_body,
    )
    return await _call(ctx, "POST", f"/api/campaigns/{campaign_id}/sequences", json_body=payload)


@leads_mcp.tool()
async def update_sequence_step(
    ctx: Context,
    campaign_id: int,
    sequence_id: int,
    subject: str | None = None,
    body: str | None = None,
    wait_days_after_previous: int | None = None,
    is_html: bool | None = None,
    preview_text: str | None = None,
    sequence_type: str | None = None,
    fallback_subject: str | None = None,
    fallback_body: str | None = None,
) -> str:
    """Update a sequence step (partial). Only provided fields change."""
    payload = _compact(
        subject=subject,
        body=body,
        wait_days_after_previous=wait_days_after_previous,
        is_html=is_html,
        preview_text=preview_text,
        sequence_type=sequence_type,
        fallback_subject=fallback_subject,
        fallback_body=fallback_body,
    )
    if not payload:
        return json.dumps({"error": "Provide at least one sequence field to update"})
    return await _call(
        ctx, "PATCH", f"/api/campaigns/{campaign_id}/sequences/{sequence_id}", json_body=payload
    )


@leads_mcp.tool()
async def delete_sequence_step(ctx: Context, campaign_id: int, sequence_id: int) -> str:
    """Delete a sequence step; remaining steps are re-indexed and the queue recalculated."""
    return await _call(ctx, "DELETE", f"/api/campaigns/{campaign_id}/sequences/{sequence_id}")


@leads_mcp.tool()
async def list_sequence_variants(ctx: Context, campaign_id: int, sequence_id: int) -> str:
    """List A/B variants of a sequence step (always empty for personalized steps)."""
    return await _call(
        ctx, "GET", f"/api/campaigns/{campaign_id}/sequences/{sequence_id}/variants"
    )


@leads_mcp.tool()
async def create_sequence_variant(
    ctx: Context,
    campaign_id: int,
    sequence_id: int,
    body: str,
    label: str = "",
    subject: str | None = None,
    is_html: bool | None = None,
    preview_text: str | None = None,
    enabled: bool = True,
) -> str:
    """Add an A/B variant to a standard sequence step. Omitted subject / is_html inherit from the step."""
    payload = _compact(
        label=label,
        subject=subject,
        body=body,
        is_html=is_html,
        preview_text=preview_text,
        enabled=enabled,
    )
    return await _call(
        ctx,
        "POST",
        f"/api/campaigns/{campaign_id}/sequences/{sequence_id}/variants",
        json_body=payload,
    )


@leads_mcp.tool()
async def update_sequence_variant(
    ctx: Context,
    campaign_id: int,
    sequence_id: int,
    variant_id: int,
    label: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    is_html: bool | None = None,
    preview_text: str | None = None,
    enabled: bool | None = None,
) -> str:
    """Update an A/B variant (partial). Only provided fields change."""
    payload = _compact(
        label=label,
        subject=subject,
        body=body,
        is_html=is_html,
        preview_text=preview_text,
        enabled=enabled,
    )
    if not payload:
        return json.dumps({"error": "Provide at least one variant field to update"})
    return await _call(
        ctx,
        "PATCH",
        f"/api/campaigns/{campaign_id}/sequences/{sequence_id}/variants/{variant_id}",
        json_body=payload,
    )


@leads_mcp.tool()
async def delete_sequence_variant(
    ctx: Context, campaign_id: int, sequence_id: int, variant_id: int
) -> str:
    """Delete an A/B variant."""
    return await _call(
        ctx, "DELETE", f"/api/campaigns/{campaign_id}/sequences/{sequence_id}/variants/{variant_id}"
    )


@leads_mcp.tool()
async def preview_campaign_email(
    ctx: Context,
    campaign_id: int,
    sequence_id: int,
    lead_id: int | None = None,
    variant_id: int | None = None,
    subject_override: str | None = None,
    body_override: str | None = None,
    is_html_override: bool | None = None,
) -> str:
    """Render a step's email (optionally for a lead / A/B variant, or with unsaved subject/body overrides)."""
    payload = _compact(
        sequence_id=sequence_id,
        lead_id=lead_id,
        variant_id=variant_id,
        subject_override=subject_override,
        body_override=body_override,
        is_html_override=is_html_override,
    )
    return await _call(ctx, "POST", f"/api/campaigns/{campaign_id}/preview", json_body=payload)


@leads_mcp.tool()
async def send_test_email(
    ctx: Context,
    campaign_id: int,
    sequence_id: int,
    to_email: str,
    lead_id: int | None = None,
    variant_id: int | None = None,
) -> str:
    """Send a rendered test of a step to to_email via the campaign's first inbox."""
    payload = _compact(
        sequence_id=sequence_id,
        to_email=to_email,
        lead_id=lead_id,
        variant_id=variant_id,
    )
    return await _call(ctx, "POST", f"/api/campaigns/{campaign_id}/send-test", json_body=payload)


# ---------------------------------------------------------------------------
# Campaign leads
# ---------------------------------------------------------------------------


@leads_mcp.tool()
async def list_campaign_leads(ctx: Context, campaign_id: int) -> str:
    """List leads enrolled in a campaign with per-campaign status, interest and activity."""
    return await _call(ctx, "GET", f"/api/campaigns/{campaign_id}/leads")


@leads_mcp.tool()
async def add_campaign_leads(
    ctx: Context,
    campaign_id: int,
    leads: list[dict[str, Any]],
    skip_duplicates: bool = True,
    verify_emails: bool = False,
    confirm_only: bool = False,
) -> str:
    """Add leads to a campaign. Each item needs email; optional name, custom_data object, status, interest,
    email_verification_status. confirm_only=true reports what would happen without enrolling."""
    if not leads:
        return json.dumps({"error": "leads array must not be empty"})
    params = {
        "skip_duplicates": _bool_param(skip_duplicates),
        "verify_emails": _bool_param(verify_emails),
        "confirm_only": _bool_param(confirm_only),
    }
    return await _call(
        ctx,
        "POST",
        f"/api/campaigns/{campaign_id}/leads",
        params=params,
        json_body=leads,
        timeout=300.0,
    )


@leads_mcp.tool()
async def import_campaign_leads_csv(
    ctx: Context,
    campaign_id: int,
    csv_content: str,
    skip_duplicates: bool = True,
    verify_emails: bool = False,
    confirm_only: bool = False,
) -> str:
    """Import leads from CSV text (header row with email, name, and any custom field columns)."""
    params = {
        "skip_duplicates": _bool_param(skip_duplicates),
        "verify_emails": _bool_param(verify_emails),
        "confirm_only": _bool_param(confirm_only),
    }
    files = {"file": ("leads.csv", csv_content.encode("utf-8"), "text/csv")}
    return await _call(
        ctx,
        "POST",
        f"/api/campaigns/{campaign_id}/leads/import",
        params=params,
        files=files,
        timeout=300.0,
    )


@leads_mcp.tool()
async def export_campaign_leads_csv(
    ctx: Context,
    campaign_id: int,
    verification_status: str = "",
    status: str = "",
    interest: str = "",
) -> str:
    """Export a campaign's leads as CSV text, optionally filtered by verification status, enrollment status or interest."""
    params: dict[str, str] = {}
    if verification_status.strip():
        params["verification_status"] = verification_status.strip()
    if status.strip():
        params["status"] = status.strip()
    if interest.strip():
        params["interest"] = interest.strip()
    r = await _request(ctx, "GET", f"/api/campaigns/{campaign_id}/leads/export", params=params)
    if r.is_error:
        return _json_response(r)
    return r.text


@leads_mcp.tool()
async def update_campaign_lead(
    ctx: Context,
    campaign_id: int,
    lead_id: int,
    status: str | None = None,
    interest: str | None = None,
    sending_paused: bool | None = None,
) -> str:
    """Update a lead's enrollment in one campaign: status, interest and/or sending_paused."""
    payload = _compact(status=status, interest=interest, sending_paused=sending_paused)
    if not payload:
        return json.dumps({"error": "Provide at least one of: status, interest, sending_paused"})
    return await _call(
        ctx, "PATCH", f"/api/campaigns/{campaign_id}/leads/{lead_id}", json_body=payload
    )


@leads_mcp.tool()
async def remove_campaign_lead(ctx: Context, campaign_id: int, lead_id: int) -> str:
    """Remove a lead from a campaign (the lead itself is kept); pending sends are dropped."""
    return await _call(ctx, "DELETE", f"/api/campaigns/{campaign_id}/leads/{lead_id}")


@leads_mcp.tool()
async def write_custom_email(
    ctx: Context,
    campaign_id: int,
    lead_id: int,
    sequence_id: int,
    subject: str | None = None,
    body: str | None = None,
    is_html: bool | None = None,
) -> str:
    """Write or update a lead's custom email for a personalized sequence step."""
    payload = _compact(subject=subject, body=body, is_html=is_html)
    return await _call(
        ctx,
        "PATCH",
        f"/api/campaigns/{campaign_id}/leads/{lead_id}/custom-email/{sequence_id}",
        json_body=payload,
    )


@leads_mcp.tool()
async def reconcile_personalized(ctx: Context, campaign_id: int) -> str:
    """Re-evaluate lead statuses against personalized steps and written custom emails."""
    return await _call(ctx, "POST", f"/api/campaigns/{campaign_id}/reconcile-personalized")


@leads_mcp.tool()
async def verify_campaign_leads(ctx: Context, campaign_id: int, reverify: bool = False) -> str:
    """Queue email verification for a campaign's unverified leads (reverify=true re-checks verified ones)."""
    return await _call(
        ctx,
        "POST",
        f"/api/campaigns/{campaign_id}/leads/verify",
        params={"reverify": _bool_param(reverify)},
    )


@leads_mcp.tool()
async def get_campaign_verification_status(ctx: Context, campaign_id: int) -> str:
    """Summarize email verification statuses for a campaign's leads."""
    return await _call(ctx, "GET", f"/api/campaigns/{campaign_id}/leads/verification-status")


@leads_mcp.tool()
async def detect_campaign_lead_providers(ctx: Context, campaign_id: int) -> str:
    """Start background MX-based email provider detection for a campaign's leads."""
    return await _call(ctx, "POST", f"/api/campaigns/{campaign_id}/leads/detect-providers")


# ---------------------------------------------------------------------------
# Campaign activity
# ---------------------------------------------------------------------------


@leads_mcp.tool()
async def list_campaign_queue(ctx: Context, campaign_id: int) -> str:
    """List a campaign's scheduled (pending) sends."""
    return await _call(ctx, "GET", f"/api/campaigns/{campaign_id}/queue")


@leads_mcp.tool()
async def list_campaign_sent_emails(ctx: Context, campaign_id: int) -> str:
    """List emails already sent by a campaign."""
    return await _call(ctx, "GET", f"/api/campaigns/{campaign_id}/sent")


@leads_mcp.tool()
async def get_campaign_step_analytics(ctx: Context, campaign_id: int) -> str:
    """Per-step analytics (sent, opens, clicks, replies, A/B variant breakdown) for a campaign."""
    return await _call(ctx, "GET", f"/api/campaigns/{campaign_id}/analytics/steps")


class _MCPAuthASGI:
    """Require the same auth as the REST API before handling MCP Streamable HTTP."""

    __slots__ = ("_inner",)

    def __init__(self, inner):
        self._inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._inner(scope, receive, send)
            return
        if scope["method"] == "OPTIONS":
            await self._inner(scope, receive, send)
            return
        raw = scope.get("headers") or []
        hdrs = {k.decode().lower(): v.decode() for k, v in raw}
        x_key = hdrs.get("x-api-key")
        auth = hdrs.get("authorization")
        user = None
        try:
            async with AsyncSessionLocal() as db:
                user = await try_resolve_user_for_mcp(db, x_api_key=x_key, authorization=auth)
                if user is not None:
                    await db.commit()
                else:
                    await db.rollback()
        except Exception:
            log.exception("mcp auth session failed")
            resp = JSONResponse({"detail": "Authentication failed"}, status_code=500)
            await resp(scope, receive, send)
            return
        if user is None:
            resp = JSONResponse({"detail": "Not authenticated"}, status_code=401)
            await resp(scope, receive, send)
            return
        await self._inner(scope, receive, send)


# Single ASGI stack for top-level Starlette Route (see main — cannot use Mount("/api/mcp"): it only
# matches /api/mcp/... with an extra segment, so /api/mcp would fall through to the SPA catch-all).
leads_mcp_http_asgi = _MCPAuthASGI(_mcp_http_handler)


@contextlib.asynccontextmanager
async def leads_mcp_lifespan():
    async with leads_mcp.session_manager.run():
        yield
