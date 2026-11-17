# Hiring Desk MCP on SAP Integration Suite

**Submitted by:** Balamurugan Nithyanantham (with Abinesh Sundar) · **Date:** 17 Nov 2026

We exposed Hiring Desk, our multi-agent HR app, as a governed MCP server through the SAP
Integration Suite MCP Gateway, following SAP's tutorial *Build and Govern MCP Servers with SAP
Integration Suite* (https://developers.sap.com/tutorials/btp-integration-suite-mcp-gateway/).
**It works end to end, and it works with Claude:** Claude Code and claude.ai both call the 5
read-only tools through SAP and get live data back from Hiring Desk. ChatGPT signs in but cannot
list tools (see Problems).

> Word and PDF versions of this report are in the same folder.

## At a glance

```
 AI agent (Claude Code, claude.ai, MCP Inspector)
     |  OAuth token from its own Developer Hub subscription
     v
 SAP MCP Gateway      /hiringDeskMCP        Integration Cell (BTP trial, ap21)
     |  5 tools generated from the API artifact
     v
 SAP API artifact     "Hiring Desk API"     policies: Authorization + Authentication
     |
     v
 BTP Destination      "HiringDesk"          Basic auth = read-only Hiring Desk integration key
     |  HTTPS
     v
 Hiring Desk (Render) /integration/v1/...   each call = single-use agent token + audit row
```

## Working with Claude

Claude is a working client of the SAP-governed MCP server today, in two ways:

| Claude client | How it connects | Result |
| --- | --- | --- |
| **Claude Code** | Remote HTTP MCP server `hiring-desk-sap`; a helper script gets and refreshes the hourly SAP OAuth token automatically | Connected; tools listed and called |
| **claude.ai** (custom connector) | OAuth sign-in with a Developer Hub subscription's client ID and secret | Connected; tested by a colleague on the free plan |

Example: asked for the jobs and the top DevOps candidates, Claude calls `get_jobs` and
`get_jobs_job_id_candidates` through SAP and answers from live Hiring Desk data. Each call
appears in SAP Monitor and in Hiring Desk's AI activity log.

## Tutorial steps

All six steps of the tutorial are done on a BTP trial (subaccount `4583c7bdtrial`, region ap21).
Hiring Desk replaced the tutorial's ServiceNow backend.

| Step | What we configured | Status |
| --- | --- | --- |
| 1. Integration Cell runtime | Enabled under Settings > Runtimes | Done |
| 2. Destination | `HiringDesk` -> `https://hiring-desk-30s9.onrender.com`, BasicAuthentication with a read-only Hiring Desk integration key | Done, Check Connection 200 |
| 3. API artifact | `Hiring Desk API`, Relative URL `/integration/v1`, base path `/hiringDesk`, 5 GET operations from our OpenAPI spec; Authorization (`ESBMessaging.send`, Trust Upstream MCP Authorization) and Basic Authentication policies | Deployed, STARTED |
| 4. MCP server | `Hiring_Desk_API_mcpServer` at `/hiringDeskMCP`, all 5 operations as tools | Deployed, STARTED |
| 5. Developer Hub | Product **Hiring Desk MCP**; one agent subscription per client (OAuth credentials) | Published |
| 6. Test | MCP Inspector (CLI mode) and a Python client: token, initialize, list tools, call tools | Passed |

Tools: `get_jobs`, `get_jobs_search`, `get_jobs_job_id_candidates`, `get_candidates_candidate_id`,
`get_interviews`. All are read-only.

## Evidence (16 Nov 2026)

| Call through the SAP gateway | Result |
| --- | --- |
| OAuth client credentials (Developer Hub subscription) | Token issued, 1 hour |
| `initialize` (MCP 2025-11-25) | Server `Hiring_Desk_API_mcpServer` |
| `tools/list` | 5 tools |
| `get_jobs` | "Found 5 jobs": AI ML Engineer, Full Stack developer, Data Analyst, DevOps Engineer, HR Business Partner |
| `get_jobs_job_id_candidates` (job 4) | DevOps: Gokul Prakash 95, Ananya Bose 70, Priyanka Menon 55, Vikram Chauhan 15 |
| Hiring Desk logs | Matching `GET /integration/v1/...` requests, HTTP 200 |

Production holds made-up test data only (5 jobs, 34 candidates, emails at example.com).

## AI clients

| Client | How it connects | Status |
| --- | --- | --- |
| Claude Code | Remote HTTP MCP server; a helper script fetches and caches the OAuth token (`scripts/sap_mcp_headers.py`) | Connected |
| claude.ai (custom connector) | OAuth sign-in with a subscription's client ID and secret | Connected (tested by a colleague) |
| MCP Inspector | Bearer token header, streamable HTTP | Passed |
| ChatGPT (custom MCP) | OAuth sign-in works after assigning role `APIInvoke`; tool discovery fails | Blocked (protocol version, below) |

## Problems hit and fixes

None of these are in the tutorial; all are documented in the repo (`docs/SAP_INTEGRATION_SUITE.md`).

| Problem | Cause | Fix |
| --- | --- | --- |
| `PKIX path building failed` when SAP called Hiring Desk | Integration Cell did not trust Render's certificate chain (Google Trust Services) | Added GTS Root R4 and WE1 to the keystore (Monitor > Keystore) |
| `Unsupported protocol version` | Gateway accepts only MCP `2025-11-25` / `2026-07-28` and rejects older versions instead of negotiating | Clients must use `2025-11-25`; this is why ChatGPT cannot list tools |
| "Developer is not registered" when subscribing | Developer Hub registration waits for approval; no email on trial | Approved our own registration in Admin Center |
| ChatGPT `invalid_scope` (`email`, then `API.invoke`) | OIDC asked for `email`; our user lacked the API invoke role | OIDC off; role collection with role `APIInvoke` assigned to the user |
| Generic "internal server error" | Real cause hidden behind an MPL ID | Monitor > Message Processing, runtime Integration Cell, Error Details |

## Security and governance

- **Read-only end to end:** the SAP tools are GET operations, and the integration key Hiring Desk
  issues can only read jobs, candidates and interviews. It is refused on the rest of the app.
- **No shared passwords:** SAP holds a revocable integration key (stored only as a hash in
  Hiring Desk), never a person's login. Each AI client has its own Developer Hub subscription,
  so one can be revoked without affecting the others.
- **Audit on both sides:** SAP Monitor logs every gateway call; Hiring Desk exchanges each call
  for a single-use `agent:integration` token and records it under AI activity.
- **Secrets** live in git-ignored local files, never in the repository.

## How to verify

1. Integration Suite > Design > package **Hiring Desk**: the API and the MCP server both show
   Deployed, Runtime Status STARTED.
2. Developer Hub: product **Hiring Desk MCP** is published.
3. With a subscription's credentials, run `npx @modelcontextprotocol/inspector`, connect to
   `https://4583c7bdtrial-0bcef6a13cd44b108f56e7cb562c6f5f.a.integration.cloud.sap/hiringDeskMCP`
   (streamable HTTP, `Authorization: Bearer <token>`), list tools and run `get_jobs`.
4. The calls appear as Completed in SAP Monitor > Message Processing.

The first call can take about a minute: the free Render instance sleeps when idle.

## Code and pull requests

Repository: https://github.com/spike-crypto/AI-Agent-Ops-Agent-IDP

| PR | What | State |
| --- | --- | --- |
| #20 | First read-only MCP endpoint | Merged |
| #22 | Integration keys and `/integration/v1` read-only API + OpenAPI for SAP; agent-token audit | Merged |
| #24 | Hotfix: production outage from an unmerged migration | Merged |
| #25 | Claude Code helper script and SAP setup guide with the problems above | Awaiting review |
| #26 | Recover background jobs cut off by a server restart | Awaiting review |

## Open items

- ChatGPT: needs an MCP version the gateway accepts, or a small version shim in front of it.
- Productive use needs the Premium or Enhanced edition of Integration Suite; this runs on a trial.
- Rotate the SAP subscription secrets used during testing.
