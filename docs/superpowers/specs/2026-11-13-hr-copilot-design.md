# HR Copilot: a chat that runs the agents through delegated identity

Status: design, waiting for Bala's approval. Date: 2026-11-13.
Context: Hiring Desk (see `docs/PROJECT_STATE.md`). FastAPI + SQLAlchemy, six scoped agents,
a DB-backed background job queue (`app/jobqueue.py`), Langfuse tracing, versioned prompts.

## 1. Why

Today the HR person drives every agent through separate buttons. Bala wants one place where
HR can ask in plain language, for example *"analyse the top 5 candidates for AI Developer"*,
and the app works out which agents to use, runs them in the background, and answers.

He also wants the agents to talk to each other **through identity**: an agent acts only with a
short-lived token issued for that one step, on behalf of the HR person, never with blanket
access. This is the "agent IDP" idea and a showcase of the project's AgentOps practice.

## 2. Goals

1. A chat panel in the HR cockpit, reachable from every page.
2. An orchestrator agent, the **Copilot**, that turns a request into steps and delegates each
   step to the agent that owns it. It never reads or writes data except through those agents
   and its own read-only lookups.
3. **Delegated tokens between agents** (OAuth 2.0 token exchange pattern, RFC 8693 style):
   every delegated step runs with a token that names the HR person (`sub`), the acting agent
   chain (`act`), the target agent (`aud`), only the scopes that step needs, a 5-minute expiry
   and a single-use id (`jti`). The target agent refuses to run without a valid token.
4. Runs in the background using the existing job queue; the chat shows each step as it happens.
5. **Anything that changes the hiring record waits for HR approval.** The Copilot prepares an
   action card ("Approve these 3 actions"); nothing changes until HR clicks Approve.
6. Full audit: every turn, token exchange, delegated run and approval is recorded in the
   database and traced in Langfuse (a conversation is one Langfuse session).

## 3. Non-goals

- Sending email. Drafts still go to the review queue, as today.
- A chat for interviewers or candidates (interviewers keep "My interviews" only).
- Streaming the answer word by word. Steps appear as they finish; the final answer appears at once.
- Real network separation of agents. They stay in one process (see section 6.4).
- Model upgrades. It must work, if slowly, on the current free OpenRouter models.

## 4. Anti-goals (would make it a failure even if it works)

- The Copilot changing a candidate's stage, or anything else, without an explicit approval click.
- An agent doing more than its token allows, or a token being reusable for another step.
- A resume's text steering the Copilot into a harmful action without HR seeing it first.
- HR not being able to tell what the AI did versus what they decided.

## 5. What HR sees

- A **"Ask Copilot"** button (Lucide `message-square-text`) in the sidebar opens a right-side
  panel on any page. Violet styling, as for all AI output.
- HR types a request. The panel shows "Working on it" and then a **step timeline**, one line per
  step as it completes: *"Screener scored 4 new resumes"*, *"Comparer compared 5 candidates"*.
  Each line can expand to show the agent, the permissions its token carried and its duration.
- The **answer** is plain text with links: candidate names open that candidate's drawer.
- If the Copilot wants to change something, it shows an **action card**: a list of the exact
  changes ("Move Rahul Menon to Not moving forward", "Draft a rejection email to Rahul Menon")
  with **Approve all** and **Dismiss**. Approving runs them (in the background) and reports
  back in the chat. Dismissing changes nothing.
- The conversation is kept, so HR can follow up ("now draft offers for the top 2").
  A **New chat** button starts over. Past conversations are listed for that HR user only.
- Example requests it must handle:
  - "Analyse the top 5 candidates for AI Developer."
  - "Screen the new resumes for Backend Engineer."
  - "Which interviews are missing feedback?"
  - "Reject the bottom 3 for AI Developer and draft their emails." (action card)
  - "Prepare interview kits for tomorrow's interviews."

## 6. How it works

### 6.1 Pieces

| Piece | Where | Job |
|---|---|---|
| `Conversation`, `Message` tables | `app/models.py`, migration `0004` | Chat history per HR user |
| `ActionProposal` table | same | Proposed changes awaiting approval (pending/approved/dismissed/done/failed) |
| `TokenGrant` table | same | Audit of every delegated token (jti, sub, actor chain, aud, scopes, issued, expires, used_at) |
| `app/identity.py` | new | Agent identities, token exchange, token verification |
| `app/agents/copilot.py` | new | The orchestrator: plan, delegate, answer |
| `app/copilot_tools.py` | new | The tool catalog the Copilot may choose from |
| `app/routers/copilot.py` | new | Chat endpoints |
| UI | `app/static/app.js`, `app.css` | Chat panel, step timeline, action cards |

### 6.2 Request flow

1. HR sends a message: `POST /copilot/conversations/{id}/messages`. The message is stored and a
   background job `copilot.turn` is queued. The response returns the job id immediately.
2. The worker runs the Copilot turn. The HR person's identity for the job is their user id and
   role, read from the database at run time (deactivation stops a queued turn).
3. The Copilot asks the model for a **plan step** in JSON: either call a tool with arguments, or
   give the final answer. It works in a loop: plan, act, read the result, plan again. Hard limits:
   at most 6 tool calls and 8 model calls per turn, and 4 minutes per turn.
4. Each tool call goes through `identity.exchange()` (section 6.4) to get a token for exactly that
   step, then runs the target agent through the existing `run_agent`, which now **requires** a
   valid token when called by another agent.
5. Each finished step is appended to the turn's progress (the job row), which the chat polls.
6. The final answer and any action card are stored as the assistant's message.

### 6.3 Tool catalog (what the Copilot can do)

Read tools (Copilot's own token, read-only scopes): `list_jobs`, `find_job(name)`,
`list_candidates(job, stage?, top?)`, `get_candidate(id)`, `list_interviews(filter)`,
`pending_drafts()`. They return compact summaries, not raw resumes.

Delegated agent tools (token exchanged per call):
`screen(job)`, `compare(candidate_ids)`, `summarize_feedback(candidate)`,
`decision_memo(candidate)`, `interview_kit(interview)`. These already only score, summarise or
draft into the review queue, so they run without an approval card.

Proposal-only tools (never executed directly; they add a line to an action card):
`propose_stage_change(candidate, stage)`, `propose_rejection_email(candidate)`,
`propose_offer_draft(candidate)`, `propose_interview(candidate, interviewer, slots)`.

Rationale for the split: drafts already wait in the review queue, so drafting is safe.
Stage changes and scheduling alter the hiring record, so they wait for an action-card approval.
Drafts proposed in a card (rejection, offer) are created only after Approve, so one click
covers "reject and draft the email" together.

### 6.4 Agent identity and token exchange

- Every agent has an identity `agent:<name>` (from the registry), including `agent:copilot`.
- Tokens are JWTs signed with a key derived from `JWT_SECRET` (HKDF with label
  "agent-delegation"), distinct from user access tokens, `typ: "agent+jwt"`.
- **Exchange 1** (start of a turn): HR's identity becomes a Copilot token:
  `sub=user:<id>`, `act={sub: agent:copilot}`, `aud=agent:copilot`,
  `scope = copilot scopes ∩ HR role scopes`.
- **Exchange 2** (each delegated step): the Copilot presents its token and asks for one target
  agent and the scopes that tool needs. The issuer checks the Copilot token, then issues
  `aud=agent:<target>`, `act={sub: agent:<target>, act: {sub: agent:copilot}}`,
  `scope = requested ∩ target agent's declared scopes ∩ Copilot token scope`, `exp = 5 min`,
  a new `jti`, and the conversation/turn ids.
- **Verification** in `run_agent` (when called by an agent): signature, `typ`, `aud` equals the
  agent being run, not expired, `jti` never used before (marked used in `TokenGrant` atomically),
  `sub` user still active with the same role. The `AgentContext` gets exactly the token's
  scopes, so an agent can do no more than its token says. Any failure is a `denied` AgentRun.
- Approving an action card repeats exchange 2 for each approved line, with HR's click as the
  authority (a fresh Copilot token is issued from the approving HR user, not the original turn).
- Honest limit: the agents share a process, so this boundary is enforced by code, not by the
  network. Its value today is least privilege per step, replay protection and a complete audit
  trail of delegation. The same token format would carry over unchanged if agents became services.
- Never logged or traced: the token itself. Logged and traced: `jti`, `aud`, actor chain, scopes,
  expiry, outcome.

### 6.5 The model

- Uses the existing `OpenRouterLLM` with its fallback list, and a new versioned prompt
  `copilot.planner@v1`. JSON mode with the existing `parse_json`; a malformed plan step counts as
  a failed model call and the next model is tried.
- The planner sees: the tool catalog with plain descriptions, the conversation so far (last 10
  messages), and tool results as compact summaries. Tool results that contain applicant text are
  wrapped and marked untrusted, as in the other agents.
- If the model cannot be reached (free-tier 429s), the turn ends with a clear message
  ("The AI is busy, try again in a minute") and the steps completed so far are kept.

### 6.6 Observability

- One Langfuse **session per conversation** (`copilot-<conversation id>`), one trace per turn
  (`copilot.turn`), with each delegated agent run nested under it.
- Each trace and delegated run carries the token's `jti`, actor chain and scopes as metadata.
- Approve/dismiss on an action card is sent as a `copilot_action` score (1 / 0) on the turn.

## 7. Constraints

- Free OpenRouter models: slow (about 40 s per call) and rate-limited. A turn of 3 to 4 model
  calls can take 2 to 3 minutes. The UI must make waiting feel normal (step timeline).
- One container on Render free tier (512 MB). No new services, no Redis.
- SQLite locally and in tests, Postgres in production; all schema changes via Alembic.
- UI rules: no emoji, Lucide icons only, violet for AI, plain sentence-case copy.
- Tests use a scripted stub model; CI never calls a real model or Langfuse.
- Production deploys are currently failing on Render's side (unknown cause, logs needed from
  Abinesh). The Copilot must be fully verifiable locally in Docker meanwhile.

## 8. Decided by Bala

- The Copilot is for the HR person, in the cockpit (not for candidates).
- It must be able to drive everything the cockpit can, through the agents.
- Changes to the hiring record are **prepared and shown for approval first**, never applied directly.
- Agents communicate through identity with exchanged tokens, and the work runs in the background.

## 9. Calls made by the designer (Bala can override)

- The approval split in 6.3 (drafting and scoring run directly; stage changes, scheduling and
  emails proposed by the Copilot wait for an action card).
- Token lifetime 5 minutes, single use; HKDF-derived signing key; per-turn limits of 6 tools,
  8 model calls, 4 minutes.
- Conversations are private to the HR user who started them.
- Chat history kept indefinitely (prototype); no deletion UI in v1.

- **Who applies an approved line** (builder check): a new `recruiter` agent with one action,
  `set_stage(candidate_id, stage)`, and one new scope `candidate:stage:write` (HR role only).
  Approved interviews run `scheduling.propose`; approved emails run `writing.rejection`/`offer`.
  Every approved line therefore runs under an exchanged token.
- **Screening's automatic `new -> screened` move is exempt** from approval: it records the AI's
  work, not a decision. Decision stages (`shortlisted`, `interviewing`, `offered`, `rejected`)
  always go through an action card.
- **Copilot scopes:** the Copilot token carries the union of the scopes it may delegate,
  intersected with the HR role. Its read tools check the read scopes on that token
  (`job:read`, `candidate:read`, `interview:read`, `feedback:read`, new `draft:read`).
  The Copilot's own code calls no write methods; writes happen only inside delegated agents.
- **Background shape:** the Copilot is itself a registered agent (`copilot`, actions `turn` and
  `approve`), so the existing job queue runs it unchanged. Delegated steps run inline in that job.
  `agent_jobs` gains a `steps` JSON list for the timeline. The 4-minute limit is checked between
  steps; `screen` from chat is capped at 10 resumes per turn.

## 10. Left to the builder

- Exact JSON shape of plan steps and tool results.
- Exact endpoint paths beyond those named here, and polling interval.
- Panel layout details within the UI rules.
- How tool summaries are worded, as long as they are compact and mark applicant text untrusted.
