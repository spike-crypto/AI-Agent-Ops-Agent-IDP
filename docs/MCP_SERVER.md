# MCP endpoint (read-only)

`POST /mcp` lets an external tool — for example **SAP CPI** — read Hiring Desk data with the
Model Context Protocol (JSON-RPC 2.0, protocol version `2025-06-18`). It exposes the six
**read** tools from `app/copilot_tools.py`. Write and delegate tools (`screen`, `compare`,
`propose_*`, `decision_memo`, `interview_kit`) are refused by name, so this surface cannot
change the hiring record.

- Code: `app/routers/mcp.py`, entry point `run_read_tool` in `app/copilot_tools.py`.
- Tests: `tests/test_mcp.py`.

## Authentication

Use the same token as the rest of the API: `POST /auth/login` with an **HR admin** account,
then send `Authorization: Bearer <access_token>` on every request.

- Missing or invalid token: `401`.
- Interviewer token: `403` (only `hr_admin` can use the surface).
- Access tokens last 15 minutes (`JWT_EXPIRY_MINUTES`), so a long-running integration must
  sign in again before expiry.
- Limit: 120 requests per minute per user (`429` with `Retry-After`).
- Audit: every `tools/call` writes an `AgentRun` row (`agent="mcp"`, action = tool name,
  scopes used, success/error, duration), visible under **AI activity** in the UI.

## Methods

| Method | Result |
|---|---|
| `initialize` | Protocol version, `tools` capability, server name |
| `notifications/initialized` | `202` with an empty body |
| `ping` | `{}` |
| `tools/list` | The six read tools with JSON schemas |
| `tools/call` | `{content, structuredContent, isError}` |

## Tools

| Tool | Arguments | Returns |
|---|---|---|
| `list_jobs` | none | Jobs with candidates counted per stage |
| `find_job` | `name` | Up to 5 jobs whose title contains the words |
| `list_candidates` | `job_id`, optional `stage`, optional `top` | Candidates, best score first, plus names not screened yet |
| `get_candidate` | `candidate_id` | Stage, score, screening strengths/gaps, interviews |
| `list_interviews` | optional `status` (`proposed`, `scheduled`, `done`, `missing_feedback`) | Interviews with feedback status |
| `pending_drafts` | none | Drafts waiting in the review queue (titles only) |

Resume text never leaves the tool layer; screening summaries derived from applicant text are
marked `untrusted_text` in the Copilot path and are not returned raw here.

## Example

```bash
APP_URL=https://hiring-desk-30s9.onrender.com

TOKEN=$(curl -s -X POST "$APP_URL/auth/login" -H 'Content-Type: application/json' \
  -d '{"email":"hr@example.com","password":"..."}' | jq -r .access_token)

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

## SAP CPI notes

- Use an **HTTPS sender adapter** with `POST /mcp`, `Content-Type: application/json` and a
  `Authorization: Bearer ${token}` header. Keep the HR admin credentials in a secure
  credential store, not in the iFlow.
- Responses are plain JSON (no server-sent events, no session id), which maps directly to a
  JSON-to-XML converter in CPI.
- Refresh the token every 15 minutes by calling `/auth/login` again; a `401` means the token
  expired or was revoked.
- The endpoint is read-only on purpose. Anything that changes data stays in the Copilot
  approval flow, where a human clicks **Approve all**.
- The free Render instance sleeps after ~15 idle minutes, so the first call of the day can
  take about a minute.
