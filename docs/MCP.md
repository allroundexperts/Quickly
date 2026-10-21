# Model Context Protocol (MCP) — Quickly Leads & Campaigns

Quickly exposes a **remote MCP server** over **Streamable HTTP** so AI clients (Cursor, Claude Desktop, etc.) can call lead and campaign tools without running a local Python MCP process. The server is part of the main FastAPI app and uses the same authentication as the REST API.

---

## Endpoint

| Item | Value |
|------|--------|
| **URL** | `{BASE_URL}/api/mcp` |
| **Transport** | Streamable HTTP (use `mcp-remote` with `--transport http-only` when connecting from stdio-only clients) |
| **Auth** | `X-API-Key: <key>` and/or `Authorization: Bearer <jwt>` on every request (same as [API.md](API.md)) |

`BASE_URL` is the value you set in the environment (e.g. `https://quickly.example.com`). The UI **Settings → MCP (AI agents)** shows the resolved URL after login.

---

## Authentication

1. **API key (recommended for agents)** — Create a key under **Settings → API Keys**. Send it as:

   ```http
   X-API-Key: <your-api-key>
   ```

2. **JWT Bearer** — Same access token as the REST API (`Authorization: Bearer …`). Tokens expire (see API docs); API keys are usually simpler for long-lived MCP setups.

MCP requests are checked before the MCP session runs. Tool calls that proxy to `/api/leads` and `/api/campaigns/...` reuse the credentials from the **incoming MCP HTTP request**, so each user/session only accesses their own data.

---

## Tools

Each tool proxies one REST endpoint, and the JSON responses are the same as those documented in [API.md](API.md). For update tools, a field you leave out stays as it is.

### Leads

| Tool | Endpoint |
|------|--------|
| `list_leads` | `GET /api/leads` (`q`, `status`, `bad_only`, `interest`; all combine with AND when set) |
| `get_lead` | `GET /api/leads/{id}` |
| `update_lead` | `PATCH /api/leads/{id}` (`name`, `enrollment_status`, `custom_data`) |
| `delete_lead` | `DELETE /api/leads/{id}` |

### Campaigns

| Tool | Endpoint |
|------|--------|
| `list_campaigns` | `GET /api/campaigns` |
| `get_campaign` | `GET /api/campaigns/{id}` |
| `campaigns_have_leads` | `GET /api/campaigns/has-leads` |
| `create_campaign` | `POST /api/campaigns` (`name`, `inbox_ids`, plus any settings) |
| `update_campaign` | `PATCH /api/campaigns/{id}` (settings: schedule, inboxes, tracking, unsubscribe, plain-text, timezone, `paused`, …) |
| `delete_campaign` | `DELETE /api/campaigns/{id}` (also deletes leads enrolled only in this campaign) |
| `duplicate_campaign` | `POST /api/campaigns/{id}/duplicate` |
| `reorder_campaigns` | `POST /api/campaigns/reorder` |

### Sequence steps and A/B variants

| Tool | Endpoint |
|------|--------|
| `list_sequence_steps` | `GET /api/campaigns/{id}/sequences` |
| `create_sequence_step` | `POST /api/campaigns/{id}/sequences` |
| `update_sequence_step` | `PATCH /api/campaigns/{id}/sequences/{sequence_id}` |
| `delete_sequence_step` | `DELETE /api/campaigns/{id}/sequences/{sequence_id}` |
| `list_sequence_variants` | `GET …/sequences/{sequence_id}/variants` |
| `create_sequence_variant` | `POST …/sequences/{sequence_id}/variants` |
| `update_sequence_variant` | `PATCH …/sequences/{sequence_id}/variants/{variant_id}` |
| `delete_sequence_variant` | `DELETE …/sequences/{sequence_id}/variants/{variant_id}` |
| `preview_campaign_email` | `POST /api/campaigns/{id}/preview` |
| `send_test_email` | `POST /api/campaigns/{id}/send-test` |

### Campaign leads

| Tool | Endpoint |
|------|--------|
| `list_campaign_leads` | `GET /api/campaigns/{id}/leads` |
| `add_campaign_leads` | `POST /api/campaigns/{id}/leads` (bulk add / enroll) |
| `import_campaign_leads_csv` | `POST /api/campaigns/{id}/leads/import` (pass the CSV as text in `csv_content`) |
| `export_campaign_leads_csv` | `GET /api/campaigns/{id}/leads/export` (returns the CSV as text) |
| `update_campaign_lead` | `PATCH /api/campaigns/{id}/leads/{lead_id}` (`status`, `interest`, `sending_paused`) |
| `remove_campaign_lead` | `DELETE /api/campaigns/{id}/leads/{lead_id}` |
| `write_custom_email` | `PATCH /api/campaigns/{id}/leads/{lead_id}/custom-email/{sequence_id}` |
| `reconcile_personalized` | `POST /api/campaigns/{id}/reconcile-personalized` |
| `verify_campaign_leads` | `POST /api/campaigns/{id}/leads/verify` |
| `get_campaign_verification_status` | `GET /api/campaigns/{id}/leads/verification-status` |
| `detect_campaign_lead_providers` | `POST /api/campaigns/{id}/leads/detect-providers` |

### Campaign activity

| Tool | Endpoint |
|------|--------|
| `list_campaign_queue` | `GET /api/campaigns/{id}/queue` |
| `list_campaign_sent_emails` | `GET /api/campaigns/{id}/sent` |
| `get_campaign_step_analytics` | `GET /api/campaigns/{id}/analytics/steps` |

Standalone lead creation is not exposed as a separate MCP tool; use `add_campaign_leads` (same rules as the REST API).

---

## Cursor and `mcp-remote`

Most MCP clients only support **stdio**. Use **[mcp-remote](https://www.npmjs.com/package/mcp-remote)** so the editor runs a small Node bridge to Quickly’s HTTPS endpoint.

### From the UI

1. Log in → **Settings → MCP (AI agents)**.
2. Create an **API key** if needed.
3. Click **Copy MCP fragment** and merge the `mcpServers` entry into your MCP config (Cursor: `~/.cursor/mcp.json`).

The fragment uses environment substitution so secrets are not embedded in JSON with awkward escaping:

```json
{
  "mcpServers": {
    "quickly-leads": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://your-domain.com/api/mcp",
        "--header",
        "X-API-Key:${QUICKLY_MCP_API_KEY}",
        "--transport",
        "http-only"
      ],
      "env": {
        "QUICKLY_MCP_API_KEY": "your-key-here"
      }
    }
  }
}
```

### JWT instead of an API key

Use a header without spaces after the colon (Cursor/Windows caveat per mcp-remote docs), and put the full `Bearer …` value in `env`:

```json
"args": [
  "-y",
  "mcp-remote",
  "https://your-domain.com/api/mcp",
  "--header",
  "Authorization:${QUICKLY_MCP_AUTH}",
  "--transport",
  "http-only"
],
"env": {
  "QUICKLY_MCP_AUTH": "Bearer eyJ..."
}
```

### Local HTTP (development only)

If Quickly is only on `http://localhost:8000`, add **`--allow-http`** after the URL in `args` (see mcp-remote README). Do not use this on untrusted networks.

---

## Setup helper API

`GET /api/settings/mcp-setup` (authenticated) returns:

| Field | Description |
|-------|-------------|
| `api_base_url` | Configured app base URL |
| `mcp_http_url` | Full MCP endpoint (`…/api/mcp`) |
| `cursor_mcp_fragment` | Ready-to-merge `mcpServers` object for `npx mcp-remote` |

See [API.md — Settings](API.md#settings) for the full Settings API.

---

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| **404 / 405** on `/api/mcp` | Old deployment without the dedicated MCP routes; upgrade Quickly. |
| **401** | Missing or invalid `X-API-Key` / Bearer token. |
| **421 Misdirected Request** / “Invalid Host header” | Fixed in current Quickly: MCP DNS rebinding checks are disabled for the public Host your reverse proxy sends. Upgrade if you still see this. |
| **Connection / TLS errors** | Check `BASE_URL`, certificate, and that the client can reach the host. |

---

## Related documentation

- [API.md](API.md) — REST reference (leads, campaigns, auth)
- [README.md](../README.md) — Install and overview
