# MCP endpoint (read-only, for direct MCP clients)

`POST /mcp` lets an MCP client (Claude, the MCP Inspector, another agent) read Hiring Desk data
with the Model Context Protocol (JSON-RPC 2.0, protocol version `2025-06-18`). It exposes the
five **read** tools an integration key may use. Write and delegate tools (`screen`, `compare`,
`propose_*`, `decision_memo`, `interview_kit`) and `pending_drafts` are refused by name, so
this surface cannot change the hiring record.

**SAP Integration Suite does not use this endpoint.** Its MCP Gateway generates its own MCP
server from the REST integration API: see `docs/SAP_INTEGRATION_SUITE.md`.

- Code: `app/routers/mcp.py`; every call goes through `app.integration.call`.
- Tests: `tests/test_mcp.py`, `tests/test_integration_api.py`.

## Authentication

Only an **integration key** works here, never a person's sign-in (a person's token unlocks the
whole API, so it must never be stored in another system).

- Create one as an HR admin: `POST /integration-keys {"name": "..."}`. The `hdk_...` key is in
  that response only; Hiring Desk stores its SHA-256 hash.
- Send `Authorization: Bearer hdk_...` (or Basic with the key as the password).
- The key does not expire. Revoke it with `POST /integration-keys/{id}/revoke`; it also stops
  if the HR admin who created it is deactivated.
- It can only read (`job:read`, `candidate:read`, `interview:read`) and is refused on the main API.
- Missing, wrong or revoked key: `401`. Limit: 120 requests per minute per key (`429`).
- Audit: each call is exchanged for a single-use `agent:integration` token (`token_grants`) and
  writes an `AgentRun` row (`agent="integration"`, the tool, the key id, the token id), shown as
  **Integration** under **AI activity**.

## Methods

| Method | Result |
|---|---|
| `initialize` | Protocol version, `tools` capability, server name |
| `notifications/initialized` | `202` with an empty body |
| `ping` | `{}` |
| `tools/list` | The five read tools with JSON schemas |
| `tools/call` | `{content, structuredContent, isError}` |

## Tools

| Tool | Arguments | Returns |
|---|---|---|
| `list_jobs` | none | Jobs with candidates counted per stage |
| `find_job` | `name` | Up to 5 jobs whose title contains the words |
| `list_candidates` | `job_id`, optional `stage`, optional `top` | Candidates, best score first, plus names not screened yet |
| `get_candidate` | `candidate_id` | Stage, score, screening strengths/gaps, interviews |
| `list_interviews` | optional `status` (`proposed`, `scheduled`, `done`, `missing_feedback`) | Interviews with feedback status |

Resume text never leaves the tool layer; screening summaries derived from applicant text are
marked `untrusted_text` in the Copilot path and are not returned raw here.

## Example

```bash
APP_URL=https://hiring-desk-30s9.onrender.com

TOKEN=hdk_...   # an integration key, see Authentication

curl -s "$APP_URL/mcp" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-06-18","capabilities":{},
                 "clientInfo":{"name":"sap-cpi","version":"1.0"}}}'

curl -s "$APP_URL/mcp" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

curl -s "$APP_URL/mcp" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call",
       "params":{"name":"list_candidates","arguments":{"job_id":1,"top":5}}}'
```

A successful `tools/call` returns:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [{"type": "text", "text": "{\"summary\": \"...\", \"data\": {...}}"}],
    "structuredContent": {"summary": "...", "data": {}},
    "isError": false
  }
}
```

`isError: true` means the call was understood but failed (for example a missing `job_id`);
the message is in `content[0].text`. Protocol errors use JSON-RPC codes `-32700` (parse),
`-32600` (invalid request), `-32601` (unknown method) and `-32602` (unknown tool or bad params).

## Notes

- Responses are plain JSON (no server-sent events, no session id).
- The free Render instance sleeps after ~15 idle minutes, so the first call can take about a
  minute.
- Anything that changes data stays in the Copilot approval flow, where a person clicks
  **Approve all**.
