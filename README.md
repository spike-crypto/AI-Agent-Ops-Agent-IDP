# Hiring Desk: HR agents with a human in charge

A prototype for **one HR person** who runs hiring end to end. Six AI agents do the
repetitive work. The HR person makes every decision, and nothing goes to a candidate
or interviewer until they have approved it.

| Agent | What it does | Data it may touch (scopes) |
|---|---|---|
| **writing** | Drafts job descriptions, rejection emails and offer letters | job:read, **job:salary:read**, candidate:read, draft:write |
| **screening** | Scores every resume against the job's requirements, with reasons | job:read, candidate:read, candidate:write |
| **scheduling** | Creates an interview with proposed slots and drafts the invite | job:read, candidate:read, interview:write, draft:write |
| **feedback** | Finds interviews with missing feedback, drafts reminders, summarises feedback, and synthesises a hiring decision memo from the interviewers' notes | job:read, candidate:read, candidate:write, interview:read, feedback:read, draft:write |
| **comparison** | Compares 2 to 4 candidates for one job side by side (read-only) | job:read, candidate:read |
| **interview_kit** | Prepares tailored interview questions and a rubric, addressed to the interviewer | job:read, candidate:read, interview:read, draft:write |

## The HR cockpit

The UI at `/` is built for the HR person, not for developers:

- **Today**: what needs you right now (drafts to approve, resumes to screen, times to confirm,
  missing feedback), a getting-started checklist, and each job's candidates by stage.
- **Candidates**: a board with one column per hiring stage. Open a candidate for the AI's
  reasons and the next step; tick 2 to 4 to compare them. Screening runs as a background
  job with "Screened n of N" progress, so a big batch never freezes the page.
- **Jobs**: write the listing, set the location, publish it, and link to the public page.
- **Review queue**: everything the AI wrote, waiting for approval. Nothing is sent automatically.
- **AI activity**: what each assistant is allowed to touch, and a log of every run.
- **Team**: add interviewers, reset passwords, turn on two-factor, sign out every device.

### Free public ATS

`/careers` is a public, no-login hiring page for published jobs. Applicants upload a
resume and it lands in the pipeline tagged "Applied on the careers page". Duplicate
applications (same email or resume) are rejected, uploads are type/size checked, and
applications are rate-limited per IP (`PUBLIC_APPLY_PER_HOUR`). Publish or hide a job
with the checkbox in **Jobs**; salary bands are never shown publicly.

Colour carries meaning: violet = written or suggested by AI, amber = waiting on you,
green = done. Icons are [Lucide](https://lucide.dev) (ISC licence), vendored in `app/static/vendor/`.

## Security model

- **Authentication:** email + password (PBKDF2, 200k iterations) → a 15-minute JWT access
  token plus a rotating refresh token in an httpOnly cookie. Access tokens are silently
  renewed, and every token carries the user's `token_version`, so deactivating a user,
  changing their password or "sign out everywhere" revokes them instantly.
- **Password reset:** self-service link (single use, 30 minutes) and an HR-issued link;
  a reset also signs out every existing session. Forgot-password gives the same answer
  whether or not the email exists.
- **Two-factor:** TOTP (any authenticator app) with one-time recovery codes; enabling or
  disabling needs the password (and a current code).
- **Rate limiting / lockout:** an account locks after `LOGIN_MAX_ATTEMPTS` wrong passwords;
  login and forgot-password are also limited per IP.
- **SSO:** optional OIDC (Google Workspace, Entra ID, Keycloak, Auth0). Set `OIDC_ISSUER`,
  `OIDC_CLIENT_ID` and `OIDC_CLIENT_SECRET` to show the SSO button; SAML can be brokered by
  the identity provider into OIDC.
- **Roles:** `hr_admin` (everything) and `interviewer` (only interviews assigned to them,
  submitting their own feedback, no agent scores).
- **Agent authorization:** an agent never gets the database. It gets an `AgentContext`
  whose methods check scopes. What an agent can do is
  `scopes it declares ∩ scopes of the signed-in user's role`. For example, the screening
  agent cannot see salary bands even when the HR admin runs it. See `app/agents/base.py`.
- **Human in the loop:** agents only create **drafts**. Approving is a human action,
  and v1 never sends email.
- **Prompt injection:** resumes are marked as untrusted input in the screening prompt.
  `sample_data/test_injection.txt` is a resume that tries to manipulate the score. Use it to test.

## Observability

- Every agent run writes an **AgentRun** audit row (who, which agent, which scopes it used,
  success/denied/error, duration). It's visible in the UI under *Agent activity*.
- Structured JSON logs with a request id per call (`LOG_JSON=true` in production), including
  login outcomes, LLM model/latency/token counts, job progress and failures. `/health` also
  runs a real one-shot LLM check and reports `llm.configured`, the model list and latency.
- With `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` set, each agent run is also a
  **Langfuse** trace (named like `screening.screen`, tagged with the agent and the user), and
  every model call inside it is recorded with its model, timing and token counts. Failed
  model attempts show up too. Prompts contain resume text: set `LANGFUSE_CAPTURE_CONTENT=false`
  to send no prompt or answer text. Tracing can never break an agent run.

## AgentOps: prompts, evals and feedback

- **Versioned prompts.** Every system prompt is a `Prompt` with a name and a version
  (`screening.system@v1`), defined next to its agent. The name and version go to Langfuse
  with every model call, so a trace always says which prompt produced it. `GET /prompts`
  (HR only) lists what is live. Changing a prompt's text without bumping its version fails
  `tests/test_prompts.py`; after a deliberate change run `python -m app.prompts --write-lock`
  and commit `prompts.lock.json`, so prompt changes show up in PR review.
- **Evals.** `python -m evals.screening` scores the sample resumes with the real model and
  checks each lands in its expected range (strong match high, designer low, the resume with a
  hidden instruction low) and that the ranking order holds. Exit code 0 = passed, 1 = a wrong
  answer, 2 = inconclusive (the model could not be reached, e.g. free-tier rate limits). Run it
  whenever the screening prompt or the model list changes. Each case is an `eval.screening`
  trace with an `eval_pass` score in Langfuse. The harness itself is tested offline in CI.
- **HR feedback as a score.** Each draft remembers the trace of the agent run that wrote it.
  When HR approves it unchanged, approves it after edits, or discards it, Langfuse gets an
  `hr_review` score of 1.0, 0.5 or 0.0 on that trace. Over time this shows which agent's
  drafts get rewritten or thrown away most.

## Run it locally

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                 # then edit it
python -m pytest -v
uvicorn app.main:app --reload --env-file .env
```

Open http://localhost:8000, sign in with `ADMIN_EMAIL` / `ADMIN_PASSWORD`, create a job,
and upload the files in `sample_data/`. API docs are at http://localhost:8000/docs.

There is **no offline fake model in the app**: without `OPENROUTER_API_KEY` the AI actions
say the AI is unavailable instead of making up an answer. Tests inject a deterministic stub
(`tests/stub_llm.py`) through FastAPI dependency overrides, so CI needs no key and no network.

Free OpenRouter models are slow and rate-limited. Screening runs in a background job and
falls through the model list on timeout, 429 or a non-JSON answer, reporting progress such
as "Screened 12 of 20"; a candidate that still fails stays in **New** so you can retry.

## Environment variables

| Name | Needed | Notes |
|---|---|---|
| `JWT_SECRET` | yes | 32+ random characters. The app refuses to start without it or with a placeholder |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `ADMIN_NAME` | first start | Creates the first HR admin if no users exist |
| `DATABASE_URL` | no | Defaults to SQLite at `./data/hr.db`; use the Postgres URL in production |
| `APP_ENV` | no | `production` turns on secure cookies and strict defaults |
| `OPENROUTER_API_KEY` | for real AI | AI actions are disabled and say so when it is missing |
| `OPENROUTER_MODELS` | no | Comma-separated: main model first, then fallbacks. Use `:free` models |
| `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `LLM_MAX_TOKENS`, `LLM_CONCURRENCY` | no | Tuning for slow free models |
| `JWT_EXPIRY_MINUTES`, `REFRESH_TOKEN_DAYS`, `MFA_TOKEN_MINUTES`, `PASSWORD_RESET_MINUTES` | no | Token lifetimes |
| `LOGIN_MAX_ATTEMPTS`, `LOGIN_WINDOW_MINUTES`, `LOCKOUT_MINUTES` | no | Account lockout |
| `PUBLIC_APPLY_PER_HOUR` | no | Per-IP cap on careers-page applications |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | no | Enables the SSO button; also `OIDC_REDIRECT_URI`, `OIDC_AUTO_PROVISION`, `OIDC_DEFAULT_ROLE` |
| `RUN_WORKER` | no | `true` (default) runs the background agent-job thread in this process |
| `LOG_JSON`, `LOG_LEVEL` | no | Structured logs for production |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | no | Turn on Langfuse tracing (both needed) |
| `LANGFUSE_HOST` | no | Defaults to `https://cloud.langfuse.com` (EU). US: `https://us.cloud.langfuse.com` |
| `LANGFUSE_CAPTURE_CONTENT` | no | `false` keeps prompt and answer text out of traces |
| `PORT` | Render | Render sets this; the container listens on it |

## Deploying (the code-along pipeline, on Render)

Follows the IT Resonance code-along, with **Render in place of Railway**:

```
Branch → Pull request → CI (test) + 1 approval → Merge to main → Deploy → Smoke test
```

- `ci.yml` runs on every PR: pytest + `docker build`. Branch protection requires it, plus one
  approval from the other person, with no bypass.
- `deploy.yml` runs on every push to main: tests again → calls the Render **deploy hook**
  with `&ref=<merge commit>` → polls `/health` until `version` equals that commit
  (Render exposes it as `RENDER_GIT_COMMIT`).

**One-time Render setup**

1. Render dashboard → **New → Blueprint** → pick this repo. `render.yaml` creates the
   `hiring-desk` web service (Docker, free plan, health check `/health`,
   **auto-deploy off**) and a free Postgres database, wiring `DATABASE_URL` into the
   service. It asks for the secret values once.
2. Service → **Settings → Deploy Hook**: copy it into `.env` as `RENDER_DEPLOY_HOOK_URL`.
   Copy the service URL into `.env` as `APP_URL` (with `https://`, no trailing slash).
3. `bash scripts/push-github-settings.sh spike-crypto/AI-Agent-Ops-Agent-IDP`
   puts them in GitHub (secret + variable).

Keep auto-deploy **off**: if Render also deploys on push, every merge deploys twice.

> **Prototype limits:** the free instance sleeps after ~15 idle minutes (the first request
> takes about a minute), free Postgres expires after 30 days, and the in-process worker means
> one worker process; run more only after replacing the job queue's poller with a shared
> queue. Use fake resumes only: free OpenRouter models may log prompts.

Schema changes are applied by Alembic at startup (`run_migrations()`), so an existing
database is upgraded in place instead of being reset. New columns need a migration in
`alembic/versions/`; `alembic.ini`/`alembic/env.py` take the URL from `DATABASE_URL`.
