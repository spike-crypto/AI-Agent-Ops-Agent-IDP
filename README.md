# Hiring Desk: HR agents with a human in charge

A prototype for **one HR person** who runs hiring end to end. Four AI agents do the
repetitive work. The HR person makes every decision, and nothing goes to a candidate
or interviewer until they have approved it.

| Agent | What it does | Data it may touch (scopes) |
|---|---|---|
| **writing** | Drafts job descriptions, rejection emails and offer letters | job:read, **job:salary:read**, candidate:read, draft:write |
| **screening** | Scores every resume against the job's requirements, with reasons | job:read, candidate:read, candidate:write |
| **scheduling** | Creates an interview with proposed slots and drafts the invite | job:read, candidate:read, interview:write, draft:write |
| **feedback** | Finds interviews with missing feedback, drafts reminders, summarises feedback | candidate:read, candidate:write, interview:read, feedback:read, draft:write |
| **comparison** | Compares 2 to 4 candidates for one job side by side (read-only) | job:read, candidate:read |

## The HR cockpit

The UI at `/` is built for the HR person, not for developers:

- **Today**: what needs you right now (drafts to approve, resumes to screen, times to confirm,
  missing feedback), a getting-started checklist, and each job's candidates by stage.
- **Candidates**: a board with one column per hiring stage. Open a candidate for the AI's
  reasons and the next step; tick 2 to 4 to compare them.
- **Review queue**: everything the AI wrote, waiting for approval. Nothing is sent automatically.
- **AI activity**: what each assistant is allowed to touch, and a log of every run.

Colour carries meaning: violet = written or suggested by AI, amber = waiting on you,
green = done. Icons are [Lucide](https://lucide.dev) (ISC licence), vendored in `app/static/vendor/`.

## Security model

- **Authentication:** email + password (PBKDF2 hashes) → a JWT bearer token. Roles are
  read from the database on every request, so deactivating a user locks them out at once.
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
- With `AGENTOPS_API_KEY` set, each run is also an **AgentOps** trace, and the LLM calls
  inside it are captured automatically.

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

Free OpenRouter models are slow (one resume took ~40 s on `google/gemma-4-31b-it:free`),
so screen a handful of resumes at a time.

Without `OPENROUTER_API_KEY`, the app uses **FakeLLM**: offline, free, deterministic
(screening scores by keyword overlap). Tests and CI always use it.

## Environment variables

| Name | Needed | Notes |
|---|---|---|
| `JWT_SECRET` | yes in production | 32+ random characters |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | first start | Creates the first HR admin if no users exist |
| `OPENROUTER_API_KEY` | for real AI | Without it the FakeLLM is used |
| `OPENROUTER_MODELS` | no | Comma-separated: main model first, then fallbacks. Use `:free` models |
| `AGENTOPS_API_KEY` | no | Turns on AgentOps tracing |
| `DATABASE_URL` | no | Defaults to SQLite at `./data/hr.db` |
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
   **auto-deploy off**) and asks for the secret values.
2. Service → **Settings → Deploy Hook**: copy it into `.env` as `RENDER_DEPLOY_HOOK_URL`.
   Copy the service URL into `.env` as `APP_URL` (with `https://`, no trailing slash).
3. `bash scripts/push-github-settings.sh spike-crypto/AI-Agent-Ops-Agent-IDP`
   puts them in GitHub (secret + variable).

Keep auto-deploy **off**: if Render also deploys on push, every merge deploys twice.

> **Prototype limits:** SQLite inside the container is wiped on every redeploy, and the free
> instance sleeps after ~15 idle minutes (first request takes ~1 minute). Use fake resumes
> only: free OpenRouter models may log prompts. Add Render Postgres before using real data.
