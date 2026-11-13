# Project state: Hiring Desk (read this first)

> For any agent or person continuing this work. Last updated 2026-11-12 on
> `fix/candidate-board-bugs`, which adds the post-review hardening (auth, Postgres/Alembic,
> background jobs, free public ATS, logging) on top of main `701fc6c`. It covers what exists,
> why, where things are, conventions, and what to do next.
> Abinesh's own task brief is `docs/HANDOFF_ABINESH.md`; user docs are `README.md`.

## 1. Who and what

- **Team 1** of the IT Resonance code-along *"Build, Dockerize and Ship: CI/CD with GitHub pull
  requests, GitHub Actions and Railway"* (source PDF: `C:\Users\balam\Downloads\CodeAlong_CICD_GitHub_Actions_Railway (1).pdf`).
- **Bala** (the user): Balamurugan Nithyanantham, GitHub `spike-crypto`, repo owner.
- **Abinesh**: GitHub `Abinesh-Sundar` (id 202484788), collaborator with push access.
- Instead of the PDF's starter API they built their own app, **Hiring Desk**: a multi-agent HR
  prototype for **one HR person** running hiring end to end. AI agents do the busywork; the HR
  person approves every email and makes every decision. Bala wants it to be genuinely useful,
  not just a workshop demo.
- **Deviation from the PDF:** deployed on **Render**, not Railway (Bala's choice).

## 2. Links

| What | Where |
|---|---|
| Repo (public) | https://github.com/spike-crypto/AI-Agent-Ops-Agent-IDP |
| Live app | https://hiring-desk-30s9.onrender.com (`/health`, `/docs`) |
| Render service | `srv-dbq6ogd6e07s73bd20q0`, created by Abinesh's Render account from `render.yaml` |
| Production database | Supabase Postgres (`aws-0-ap-southeast-2.pooler.supabase.com`), set as Render `DATABASE_URL` on 2026-11-13. The earlier Render Postgres (`dpg-dbqoa0tbedkc73e8oaf0-a`) expired and made three deploys crash at startup. Local `.env` deliberately stays on SQLite |
| Render API | `RENDER_API_KEY` in local `.env` (git-ignored): deploy status and logs via `api.render.com/v1/services/<id>/deploys` and `/v1/logs` |
| Production test users | 1 HR admin + 2 interviewers (made-up), logins in git-ignored `.env.prod-test-users` |
| Local project | `D:\Projects\Agent IDP` (Windows 11, PowerShell + Git Bash) |

## 3. Architecture (one container plus Postgres)

- **Backend:** FastAPI + SQLAlchemy 2 (`app/`). Python 3.12. SQLite for tests and local dev;
  Postgres (psycopg) in production via `DATABASE_URL` (`app/db.py` normalises `postgres://`).
- **Schema:** Alembic is the only thing that changes the schema (`alembic/versions/`, `0001`
  is the pre-Alembic baseline). `run_migrations()` (`app/migrations.py`) runs at startup and
  handles a fresh DB, a pre-Alembic DB (stamped 0001 then upgraded in place) and a current DB.
  Verified by `tests/test_migrations.py`, including a legacy DB with data.
- **Auth** (`app/routers/auth.py`, `app/security.py`, `app/deps.py`, `app/totp.py`, `app/ratelimit.py`):
  PBKDF2-SHA256 passwords; 15-minute JWT access token (`ver` claim) + rotating opaque refresh
  token in an httpOnly cookie; server-side logout, logout-all and reset revoke via
  `users.token_version`. Self-service and HR-issued password resets (single-use, 30 min),
  TOTP MFA with hashed recovery codes and a short-lived `scope: mfa` challenge token, account
  lockout plus per-IP limits on login/forgot/apply, optional OIDC SSO (`app/oidc.py`, discovery
  + signed state/nonce cookie + JWKS). Roles: `hr_admin`, `interviewer`; role read from DB each
  request, deactivation locks out at once. `JWT_SECRET` is required at startup (no random
  per-process fallback).
- **Agent jobs** (`app/jobqueue.py`, `POST/GET /agent-jobs`): screening/compare/kit runs as a
  `AgentJob` row processed by an in-process worker thread (`RUN_WORKER=true`), with progress,
  cancellation and `result` JSON. The worker checkpoints after every resume (`ctx.checkpoint()`
  in `base.py`) so a crash or cancel keeps finished work. `FOR UPDATE SKIP LOCKED` on Postgres.
  Free models are retried across the model list; non-JSON answers are treated as a failed
  attempt and the next model tries (`tests/test_llm.py`).
- **Free ATS** (`app/routers/public.py`, `app/static/careers.html`): public `/careers` page and
  `/public/jobs`, `/public/jobs/{id}/apply`, no login. Applications land with
  `candidates.source="careers"`; duplicate email/resume rejected; upload validation;
  per-IP rate limit.
- **Logging** (`app/logging_config.py`): structured logs with a request id per call; JSON in
  production (`LOG_JSON=true`), human-readable locally.
- **Agents** (`app/agents/`). Agents never touch the DB: they get an `AgentContext` (`base.py`)
  whose methods check scopes. Effective scopes = agent's declared scopes ∩ role's scopes.
  `runner.py` does the role check, builds the context, wraps a Langfuse trace and always writes
  an `AgentRun` audit row (success/denied/error). Registered in `registry.py`.

  | Agent | UI name | Actions | Scopes |
  |---|---|---|---|
  | writing | AI Writer | job_description, rejection, offer | job:read, job:salary:read, candidate:read, draft:write |
  | screening | AI Screener | screen | job:read, candidate:read, candidate:write |
  | scheduling | AI Scheduler | propose | job:read, candidate:read, interview:write, draft:write |
  | feedback | AI Follow-up | chase, summarize, synthesize (decision memo, Abinesh) | job:read, candidate:read, candidate:write, interview:read, feedback:read, draft:write |
  | comparison | AI Comparer | compare (2-4 ids) | job:read, candidate:read (read-only). Bala's Part H |
  | interview_kit | AI Interview Kit | prepare (interview_id) | job:read, candidate:read, interview:read, draft:write. Abinesh's Part H |

- **Human in the loop:** agents only create `Draft` rows (status pending). `POST /drafts/{id}/approve`
  is the human step. The app never sends email.
- **LLM:** `app/llm.py`. `OpenRouterLLM` only (OpenAI-compatible client, model fallback list,
  retries). **There is deliberately no fake model in `app/`**: without a key the AI says it is
  unavailable. Tests inject a stub via `app.dependency_overrides[get_llm_optional]`
  (`tests/stub_llm.py`), so CI needs no key and no network. Current free models:
  `google/gemma-4-31b-it:free,google/gemma-4-26b-a4b-it:free,nvidia/nemotron-3.5-lightning:free`.
  Free models are slow and rate-limited (observed 429s); the fallback list is tried on timeout,
  429, empty or non-JSON answers. `/health` runs a real one-shot check and reports latency.
- **Observability:** `app/observability.py`, Langfuse (SDK 5.x), replaced AgentOps on 2026-11-13.
  Off unless `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` are set. `trace()` wraps each agent run
  (called from `runner.py`); `generation()` wraps each model attempt in `OpenRouterLLM.complete`.
  Screening copies the context into its thread pool so model calls stay under the run's trace.
  `LANGFUSE_CAPTURE_CONTENT=false` sends no prompt/answer text. Covered by
  `tests/test_observability.py` with an in-memory exporter; **not yet seen working against a
  real Langfuse project** (needs keys).
- **Prompt-injection guard:** resumes are delimited and marked untrusted; `sample_data/test_injection.txt`
  tries to force a score of 100. Verified with the real model: it scored 0.
- **UI (the HR cockpit)** at `/`: `app/static/index.html`, `app.css`, `app.js`, vanilla JS, no
  build step. Icons: Lucide 1.61.0 vendored at `app/static/vendor/lucide.min.js` (`icon("name")`).
  Views: Today (`GET /dashboard`), Candidates (stage board, drawer, compare, background
  screening with "Screened n of N"), Jobs (location, publish/hide, careers link), Interviews
  (with an "Interview kit" button per row, also in the candidate drawer), Review queue,
  AI activity, Team (reset password, 2FA setup/recovery codes, sign out everywhere),
  How it works; interviewers see only "My interviews". Sign-in supports silent token renewal,
  MFA, forgot/reset password and SSO when configured; a separate public `careers.html`
  serves the free ATS.
  Design rules: **no emoji**, Lucide icons only, violet = AI, amber = waiting on you, green = done,
  plain sentence-case copy for HR people. Checked in Edge: light, dark, 390 px phone width.
- **Health/version:** `/health` returns `version` from `app/version.txt` if present, else
  `RENDER_GIT_COMMIT`, else `dev`. The smoke test compares it with the merge commit.

**HR Copilot (added 2026-11-13, spec `docs/superpowers/specs/2026-11-13-hr-copilot-design.md`):**
- `app/identity.py`: delegated agent tokens (JWT, `typ agent+jwt`, HKDF key from `JWT_SECRET`,
  300 s, single-use `jti`, `act` chain). `issue_for_user` (exchange 1), `exchange` (exchange 2),
  `verify` (spends the token). `run_agent(..., delegation=token)` grants exactly the token's scopes;
  `agent_runs.delegation` records jti/actors/scopes. `Agent.delegable_scopes` = scopes carried only to
  hand on (Copilot); its own `scopes` are read-only.
- `app/agents/recruiter.py`: only holder of `candidate:stage:write`.
- `app/copilot_tools.py`: 6 read, 5 delegate, 4 propose tools. `app/agents/copilot.py`: plan-act
  loop (`turn`) and `approve`. `app/routers/copilot.py`: conversations, messages (background job),
  approve/dismiss (atomic). Migration `0004`: token_grants, conversations, messages,
  action_proposals, agent_jobs.steps, agent_runs.delegation.
- UI: "Ask Copilot" panel in `app/static/app.js` (`copilot*` functions).
- Tests: `test_identity.py`, `test_recruiter.py`, `test_copilot_tools.py`, `test_copilot_agent.py`,
  `test_copilot_api.py` (scripted planner `tests/stub_llm.ScriptedLLM`).

**AgentOps practices (added 2026-11-13):**
- **Prompt versioning:** `app/prompts.py`. Each system prompt is `Prompt(name, version, text)` (a
  `str` subclass) defined in its agent file; 8 prompts, all v1. `OpenRouterLLM.complete` sends
  `prompt_name`/`prompt_version` as Langfuse generation metadata. `prompts.lock.json` + 
  `tests/test_prompts.py` fail if text changes without a version bump
  (`python -m app.prompts --write-lock` after a deliberate change). `GET /prompts` is HR-only.
- **Sessions:** `runner.session_for` gives each run a Langfuse session id per job opening
  (`job-<id>`, resolved from `job_id`, `candidate_id`, `candidate_ids` or `interview_id`); runs that
  span jobs (feedback.chase) have none. Each eval run is one `eval-screening-<timestamp>` session.
- **Evals:** `evals/screening.py` + `evals/screening_cases.json` (6 sample resumes with expected
  score ranges, 3 ordering checks). `python -m evals.screening` uses the real model; exit 0/1/2 =
  passed / wrong answer / inconclusive. Not run in CI (needs the real model); the harness is
  tested with the stub model.
- **HR feedback scores:** `drafts.trace_id` (migration `0003`) stores the Langfuse trace that wrote
  a draft; `app/routers/drafts.py` sends `hr_review` = 1.0 / 0.5 / 0.0 for approved unchanged /
  approved after edits / discarded.

## 4. CI/CD (matches the PDF, Render instead of Railway)

- `.github/workflows/ci.yml`: on PR to main, job **`test`**: pytest + `docker build`.
  Branch protection depends on that job name; don't rename it.
- `.github/workflows/deploy.yml`: on push to main (concurrency group `deploy`):
  test → `curl -X POST "$RENDER_DEPLOY_HOOK_URL&ref=$SHA"` → smoke test polling
  `$APP_URL/health` up to 48 × 15 s until `version == SHA`.
- GitHub settings: secret `RENDER_DEPLOY_HOOK_URL` (set by Abinesh), variable `APP_URL`
  (`https://hiring-desk-30s9.onrender.com`). Abinesh also added an unused secret `APP_URL`; harmless.
- `render.yaml`: Docker web service `hiring-desk` (free plan, `healthCheckPath: /health`,
  `autoDeployTrigger: off`) plus a free Postgres database wired in as `DATABASE_URL`, with
  `APP_ENV=production` and `LOG_JSON=true`. Render must not deploy on its own, or every merge
  deploys twice; confirm in the dashboard.
- **Branch protection on main:** PR required, 1 approval, `test` check, `enforce_admins` on.
  Nobody approves their own PR.
- **Branch housekeeping:** merging a PR deletes its head branch automatically
  (`delete_branch_on_merge` on). `.github/workflows/cleanup-branches.yml` deletes older merged
  branches on demand; it runs in the `branch-cleanup` environment, which requires Abinesh's
  approval (no admin bypass) and refuses `main`, protected branches and branches without a
  merged PR.
- `scripts/push-github-settings.sh owner/repo` copies the hook + APP_URL from `.env` to GitHub.

## 5. Progress against the PDF

| Part | Status |
|---|---|
| A repo + collaborator | Done |
| B/C app + Dockerfile, skeleton pushed | Done |
| D CI via PR #1 | Done, merged |
| E protect main | Done (PR + 1 approval + `test` + no bypass, verified 2026-11-12) |
| F hosting (Render) | Done |
| G deploy workflow via PR #2 | Done, approved by Abinesh, Deploy green incl. smoke test |
| H one feature each + merge conflict | **Done, with one caveat** (see below) |
| I break a test on purpose | **Done** (PR #8, Bala): CI red on `assert 'ok' == 'broken'`, merge refused; fixed, green, PR closed unmerged |

PR history for Part H:

| PR | Author | Reviewed by | Merged by | Notes |
|---|---|---|---|---|
| #5 interview kit v1 (old UI) | Abinesh | Bala: approved | Bala | Deployed `8384062` |
| #6 kit integrated into cockpit | Abinesh | Bala: approved | Abinesh | Contains all of #3 plus Abinesh's conflict resolution (`f0b1a88`, `registry.py` + `index.html`). Deployed `bc46c9e` |
| #3 AI Comparer + cockpit UI | Bala | Abinesh: **comment only**, not an approval | auto-marked merged when #6 landed | |

Caveat: Bala's feature reached main through #6, which Bala approved himself; Abinesh never
formally approved #3. Everything else in "Done when" is met: main protected, both features
merged through PRs, conflict resolved (by Abinesh), every merge produced a green Deploy run,
`/health` shows the latest commit (`701fc6c`). Issue #4 is closed.

After Part H, PRs #7 (docs), #9 (interview-kit and board fixes, approved by Abinesh) and #10
(approval-gated branch cleanup, approved by Abinesh) merged; every Deploy run was green.

## 6. Conventions (important)

- **Commit identity:** author `Balamurugan Nithyanantham <84909177+spike-crypto@users.noreply.github.com>`
  (set in the repo's local git config). Bala's commits carry
  `Co-authored-by: Abinesh <202484788+Abinesh-Sundar@users.noreply.github.com>`.
- **Never add AI attribution** (no `Co-Authored-By: Claude`, no "Generated with Claude Code").
  Bala asked for Claude to be removed from contributors; history was rewritten once to do it.
  A local tag `backup/before-claude-removal` holds the old history (never pushed).
- Work on `feature/<name>-<thing>` branches, open a PR, the other person approves.
- On Windows PowerShell, `git commit -F -` with a here-string fails silently: write the message
  to a file in the scratchpad and use `git commit -F <file>` (or use Git Bash).
- `.gitattributes` forces LF line endings (shell scripts and workflows run on Linux).
- `railway`-specific advice in the PDF no longer applies (no `railway up`, no `version.txt` step).

## 7. Secrets and local environment

- **`.env`** (git-ignored, never commit): `JWT_SECRET` (required, 32+ chars),
  `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `ADMIN_NAME`, `OPENROUTER_API_KEY` (+ commented spares;
  deliberately no key rotation, which may break OpenRouter's terms), `OPENROUTER_MODELS`,
  `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` (empty), `DATABASE_URL`, `RENDER_DEPLOY_HOOK_URL` (empty locally; the real
  value is only in GitHub secrets / Abinesh), `APP_URL`. `.env.example` lists every optional
  tuning key (token lifetimes, lockout, OIDC, logging). The keys were pasted in chat once;
  rotate them if that session is ever shared.
- The live Render app's admin login is whatever Abinesh entered when creating the Blueprint.
- Python: `uv` + venv at `.venv` (`.venv\Scripts\python.exe -m pytest -q`; the system `python`
  alias is broken, use the venv or `uv`).
- Docker: image `team-api`, container `hiring-desk` on port 8000 with `--env-file .env` and
  volume `hiring-desk-data:/code/data`. Rebuild:
  `docker build -t team-api . ; docker rm -f hiring-desk ; docker run -d --name hiring-desk -p 8000:8000 --env-file .env -v hiring-desk-data:/code/data team-api`
- Screenshot tooling used for UI checks: `playwright-core` + installed Edge (`channel: "msedge"`),
  script kept in the session scratchpad, not in the repo.

## 8. Known limits

- One container runs one web process and one in-process job worker. Horizontal scale needs a
  shared queue (the DB poll and `SKIP LOCKED` are in place; the per-process worker loop is not).
- Render free Postgres expires after 30 days and the free web instance sleeps after ~15 min.
- Free OpenRouter models are slow and rate-limited (429s seen in normal use). Default `gemma`
  models take tens of seconds per resume; the fallback list absorbs most of it.
- Prototype: fake resumes only (`sample_data/`); free models may log prompts.
- Interview times are naive datetimes (no timezone handling).
- Langfuse verified against a real project on 2026-11-13 (traces, prompt versions, scores).
- Evals cover only the screening agent. The first real run (2026-11-13) was inconclusive: 3 of 6
  cases hit OpenRouter free-tier rate limits; the 3 that ran passed. Re-run when limits reset.
- No cost tracking or alerting on failures yet (rate-limit errors are only visible in Langfuse/logs).
- Abinesh's older local commit `f0b1a88` uses `abineshsundar@email.com`, so it is not linked to
  his GitHub profile; all his later commits are linked.

No open bugs: the interview-kit and candidate-board issues (kit addressed to the candidate,
missing prompt-safety rules, `puzzle` icon, card overflow, file-name titles, duplicate uploads,
unbatched screening) were fixed in PR #9 and are live.

## 9. Next steps (in order)

1. Open a PR from `fix/candidate-board-bugs` and get Abinesh's approval (the hardening touches
   auth, deploy config and shared UI, so it must go through review). On merge, Render creates
   the Postgres database from `render.yaml`; the admin login on the live app stays whatever
   Abinesh entered when creating the Blueprint.
2. Housekeeping after that merge: Bala runs the `cleanup-branches` workflow; Abinesh approves
   the `branch-cleanup` deployment, and it deletes `setup/deploy`, `feature/bala-cockpit-ui`
   and `feature/abinesh-interview-kit`; keep `test/break-it-on-purpose` as the Part I record.
3. Optional, to make the review record clean: Abinesh adds an approving review to the merged
   PR #3.
4. Add the Langfuse keys in Render (Abinesh's account; local `.env` already has them); add OIDC settings to turn on SSO.

## 10. How to verify quickly

```bash
.venv/Scripts/python.exe -m pytest -q          # 100 tests, all offline
curl -s https://hiring-desk-30s9.onrender.com/health   # includes a real LLM check
gh pr list --repo spike-crypto/AI-Agent-Ops-Agent-IDP --state all
gh run list --repo spike-crypto/AI-Agent-Ops-Agent-IDP --limit 5
gh api repos/spike-crypto/AI-Agent-Ops-Agent-IDP/branches/main/protection --jq .required_status_checks.contexts
```
