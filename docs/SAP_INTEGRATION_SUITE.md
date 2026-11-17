# Hiring Desk in SAP Integration Suite (MCP Gateway)

This follows SAP's tutorial *Build and Govern MCP Servers with SAP Integration Suite*
(<https://developers.sap.com/tutorials/btp-integration-suite-mcp-gateway/>), with Hiring Desk in
place of ServiceNow. SAP builds and governs the MCP server; Hiring Desk is the backend API
behind it.

```
AI agent ──OAuth (Developer Hub)──> SAP MCP Gateway ──> API artifact ──Destination──> Hiring Desk
                                    (Integration Cell)                   (integration key)   /integration/v1
```

Who controls what:

| Layer | Controls |
|---|---|
| Developer Hub subscription | Which AI agent may call the MCP server (OAuth client credentials per agent) |
| MCP Gateway / API policies | Authentication, authorization, rate limits, monitoring in SAP |
| BTP Destination | Holds the Hiring Desk **integration key**; nobody else sees it |
| Hiring Desk | The key can only read. Every call becomes a single-use `agent:integration` token and an AgentRun row (**AI activity** page). Revoke the key and SAP is cut off at once. |

The steps say which values come from the tutorial and which are ours. SAP renames menus now and
then: if a label differs, look for the nearest match.

## 0. Hiring Desk side (done once, by an HR admin)

1. Create a key. It is shown **once**; store it straight in the BTP Destination (step 2), never in
   chat, email or Git.

   ```bash
   APP=https://hiring-desk-30s9.onrender.com
   TOKEN=$(curl -s -X POST $APP/auth/login -H 'Content-Type: application/json' \
     -d '{"email":"<hr admin email>","password":"<password>"}' | jq -r .access_token)
   curl -s -X POST $APP/integration-keys -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"name":"SAP Integration Suite"}'
   # -> {"id": 1, "key": "hdk_...", "scopes": ["candidate:read","interview:read","job:read"], ...}
   ```

2. Check it works (this also wakes the free Render instance, which sleeps after ~15 idle minutes):

   ```bash
   curl -s $APP/integration/v1/jobs -H "Authorization: Bearer hdk_..."
   ```

3. Download the OpenAPI file SAP will import: `$APP/integration/v1/openapi.json` (public; it only
   describes the API). It lists five read operations: `listJobs`, `findJobs`, `listCandidates`,
   `getCandidate`, `listInterviews`.

Revoke a key: `POST /integration-keys/{id}/revoke`. List keys: `GET /integration-keys`.

## 1. Integration Cell runtime

Done. (Tutorial: Integration Suite → **Settings → Runtimes**, enable the Integration Cell;
provisioning can take up to 30 minutes.)

## 2. Destination (BTP Cockpit → your subaccount → Connectivity → Destinations)

| Field | Value |
|---|---|
| Name | `HiringDesk` |
| Type | `HTTP` |
| URL | `https://hiring-desk-30s9.onrender.com` (no path, no trailing slash) |
| Proxy Type | `Internet` |
| Authentication | `BasicAuthentication` |
| User | `integration` (any text; Hiring Desk ignores it) |
| Password | the `hdk_...` key from step 0 |
| Additional property | `IntegrationCell.Include` = `true` (from the tutorial; it lets the Integration Cell use this destination) |

Hiring Desk accepts the key as the Basic password, so the tutorial's BasicAuthentication works
unchanged. Use **Check Connection**: any HTTP answer (even 404 on `/`) means BTP can reach it.

## 3. API artifact

1. Integration Suite → **Design** → create an Integration Package, for example `Hiring Desk`.
2. Add an **API** artifact with source **API Provider** (the tutorial's choice), pointing at the
   `HiringDesk` destination.
   - Name: `Hiring Desk API`
   - Base path: `/hiringDesk`
3. In the API Designer, replace the spec with the contents of `openapi.json` from step 0. Keep
   the paths as they are (`/integration/v1/...`): the destination URL has no path, so the target
   becomes `https://hiring-desk-30s9.onrender.com/integration/v1/jobs` and so on.
4. Policies, as in the tutorial:
   - **Authorization** step with scope `ESBMessaging.send` and **Trust Upstream MCP Authorization**.
   - **Authentication** step with **Basic** (it uses the destination's credentials).
5. Save and **deploy to the Integration Cell**.

## 4. MCP Server artifact

1. In the same package, add an **MCP Server** artifact based on `Hiring Desk API`.
   - MCP Path: `/hiringDeskMCP`
   - Version: `1.0.0`
2. Select all five operations. Their summaries and descriptions become the tool descriptions
   AI agents read, which is why they are written in plain sentences in the spec.
3. Deploy.

## 5. Product and agent subscription (Developer Hub)

1. Developer Hub admin center → the **Integration Cell** business system → create a **product**
   from the `hiringDeskMCP` server.
2. Create a **subscription** for your agent. It gives a client id and secret (OAuth client
   credentials). One subscription per agent means you can cut off one agent without touching
   the others.

## 6. Test with the MCP Inspector

```bash
npx @modelcontextprotocol/inspector
```

1. Get a bearer token from the subscription's token URL with the client id and secret
   (client-credentials grant, as in the tutorial).
2. Add a server: transport **streamable-http**, the gateway URL ending in `/hiringDeskMCP`,
   header `Authorization: Bearer <token>`.
3. **List tools**: you should see the five operations. Call `listJobs`, then
   `listCandidates` with a `job_id` from the result.
4. In Hiring Desk, open **AI activity**: each call shows as **Integration** with the key and
   the single-use token it ran with.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| First call times out, the next one works | Render's free instance was asleep. Call `/health` first, or retry. |
| `401 A valid integration key is required` | Key mistyped, revoked, or its HR admin was deactivated. Create a new key and update the destination password. |
| `404` from Hiring Desk | Path doubled or missing: the destination URL must have no path, the spec keeps `/integration/v1/...`. |
| `429` | More than 120 calls a minute on one key. |
| Tools missing in the Inspector | Operations not selected in step 4, or the MCP server not redeployed after a spec change. |

## 7. Connect Claude Code (a real AI agent)

The subscription uses client credentials, and the token lasts an hour, so Claude Code gets
its header from a helper instead of a pasted token.

1. Save the subscription's credentials in `.env.sap-oauth` at the repo root (git-ignored):

   ```
   SAP_TOKEN_URL=https://<subaccount>.authentication.<region>.hana.ondemand.com/oauth/token
   SAP_CLIENT_ID=<key>
   SAP_CLIENT_SECRET=<secret>
   ```

2. Check the helper prints a header: `python scripts/sap_mcp_headers.py`. It caches the token
   in `.sap-token-cache.json` (git-ignored) until a minute before it expires.
3. Register the server for yourself only (`--scope local` keeps it out of the repo). The
   helper paths must be absolute:

   ```bash
   claude mcp add-json --scope local hiring-desk-sap '{"type":"http",
     "url":"https://<your-integration-cell-host>/hiringDeskMCP",
     "headersHelper":"\"<abs path>/python\" \"<abs path>/scripts/sap_mcp_headers.py\""}'
   claude mcp get hiring-desk-sap        # Status: Connected
   ```

4. Start a new Claude Code session (servers load at start), run `/mcp`, and ask for jobs or
   candidates "using hiring-desk-sap". Calls show in SAP Monitor and under **AI activity**.

Give each person or agent **its own Developer Hub subscription**, so one can be revoked without
touching the others.

## Gotchas we hit (2026-11-16)

| Symptom | Fix |
|---|---|
| `PKIX path building failed ... unable to find valid certification path` in Monitor | The Integration Cell does not trust Render's certificate chain (Google Trust Services). Monitor → Manage Security → **Keystore** → Add → Certificate: add **GTS Root R4** and **WE1** (`openssl s_client -connect <host>:443 -showcerts` prints them). The Cockpit's Check Connection passes anyway, because it uses a different trust store. |
| `Unsupported protocol version (supported versions: 2026-07-28,2025-11-25)` | The gateway speaks newer MCP versions only; set the client's protocol version to `2025-11-25`. |
| Subscription fails: "developer is not registered in the Developer Hub" | Click **Register**, then approve your own request in **Admin Center**. No email arrives on a trial. |
| `get_jobs` says "internal server error" with an MPL ID | Look the ID up in Monitor → Message Processing (runtime **Integration Cell**, type **All**). **Error Details** has the real cause. |
| Tools are named `get_jobs_job_id_candidates` | SAP names tools after the URL path, not the `operationId`. Rename them in the MCP server's **MCP Configuration → Tools** if you want shorter names. |

## Going further

- **Trial vs production:** the tutorial notes that productive use needs the Premium or Enhanced
  edition of Integration Suite.
- **Direct MCP clients** (Claude, other agents) can skip SAP and call Hiring Desk's own
  `POST /mcp` with an integration key: see `docs/MCP_SERVER.md`. SAP does not use it.
- **Writes stay inside Hiring Desk.** Stage changes and emails go through the Copilot's approval
  cards; no integration key can make them.
