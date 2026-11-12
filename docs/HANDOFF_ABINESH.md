# Handoff: Hiring Desk (for Abinesh and his coding agent)

> **How to use this file:** read it once yourself, then give it to your coding agent
> (Claude Code, Cursor, Copilot…) as the first message, with:
> *"Read docs/HANDOFF_ABINESH.md and README.md, then help me with my tasks. Follow the
> rules in section 6 exactly."*

---

## 1. What we are building and why

**Hiring Desk** is our team's (Team 1: Bala + Abinesh) project for the IT Resonance
code-along *"Build, Dockerize and Ship: CI/CD with GitHub PRs, GitHub Actions and a cloud host"*.
We replaced the starter API with our own app. We deploy to **Render** instead of Railway.

The app is for **one HR person** who runs hiring end to end. Six AI agents ("assistants" in
the UI) do the repetitive work, and the HR person makes every decision:

| Agent (code) | Name in the UI | Job |
|---|---|---|
| `writing` | AI Writer | Drafts job descriptions, rejection emails, offer letters |
| `screening` | AI Screener | Scores each resume against the job's requirements, with reasons |
| `scheduling` | AI Scheduler | Creates an interview with proposed time slots, drafts the invite |
| `feedback` | AI Follow-up | Finds interviews missing feedback, drafts reminders, summarises feedback |
| `comparison` | AI Comparer | Compares 2 to 4 candidates side by side. Read-only (Bala's Part H feature) |
| `interview_kit` | AI Interview Kit | Tailored questions + rubric for one interview (your Part H feature, merged) |

Three ideas run through the whole codebase. **Don't break them:**

1. **Human in the loop.** Agents only create *drafts* (`Draft` table, status `pending`).
   Only a human approves. The app never sends email itself.
2. **Agents have least privilege.** An agent never touches the database directly. It gets
   an `AgentContext` (`app/agents/base.py`) whose methods check *scopes*. An agent can
   only do `scopes it declares ∩ scopes of the signed-in user's role`. Example: the
   screening agent can never see a job's `salary_band`.
3. **Everything is observable.** `app/agents/runner.py` wraps every run in an AgentOps
   trace and writes an `AgentRun` audit row (success, denied or error), shown in
   *Agent activity* in the UI.

It is a **prototype**: fake resumes only (see `sample_data/`), and the SQLite database resets
on every deploy.

## 2. Status: what Bala already built (on `main`)

- FastAPI backend, SQLite via SQLAlchemy 2.
- **The HR cockpit UI** at `/`: `app/static/index.html` (shell), `app.css` (design tokens and
  components), `app.js` (all views, vanilla JS, no build step). Icons come from **Lucide**,
  vendored at `app/static/vendor/lucide.min.js`: write `${icon("name")}` and the page paints it.
  Views: Today (what needs you), Candidates (stage board + candidate drawer + compare),
  Jobs, Interviews, Review queue (approve drafts), AI activity (permissions + log), Team, How it works.
- `GET /dashboard` (`app/routers/dashboard.py`) powers the Today view.
- **Auth:** email + password (PBKDF2) → JWT bearer token. Roles: `hr_admin`, `interviewer`.
  `app/deps.py` has `get_current_user`, `require_hr`, `require_role(...)`.
- The 6 agents, the scoped `AgentContext`, the runner, the AgentRun audit log, AgentOps.
- LLM via **OpenRouter** free models (`app/llm.py`), with a `FakeLLM` that works offline.
  **Tests and CI always use FakeLLM.** Never call a real API from tests.
- 40 pytest tests (`tests/`, including 9 in `test_board_fixes.py` and 6 in
  `test_interview_kit.py`), Dockerfile, `render.yaml`.
- CI/CD per the code-along PDF: `ci.yml` (job `test`: pytest + docker build on every PR) and
  `deploy.yml` (on push to main: test → Render deploy hook with the commit SHA → smoke test
  that waits for `/health` to report that commit).
- `main` is protected: PR required, **1 approval**, green `test` check, no bypass.

## 3. Map of the code

```
app/
  main.py              FastAPI app, /health, router includes ("Feature routers" section at the bottom)
  config.py            all settings from env vars
  models.py            tables: User, Job, Candidate, Interview, Feedback, Draft, AgentRun
  security.py, deps.py login, JWT, role checks
  llm.py               OpenRouterLLM + FakeLLM + parse_json
  observability.py     AgentOps (optional, never crashes the app)
  agents/
    base.py            SCOPES, ROLE_SCOPES, AgentContext (the only data access agents get), Agent base class
    registry.py        AGENTS = {...}: every agent is registered here  <-- shared file
    runner.py          run_agent(): role check, scoped context, trace, audit row
    writing.py, screening.py, scheduling.py, feedback.py, comparison.py, interview_kit.py
  routers/             auth, dashboard, jobs (+candidates), interviews, drafts, agents
  static/              index.html, app.css, app.js (the cockpit UI), vendor/lucide.min.js (icons)
tests/                 conftest.py has fixtures: client, hr, interviewer, job
```

Agents are called over HTTP: `POST /agents/{agent}/{action}` with a JSON body of params.
Each action is a method on the agent class, listed in its `actions` tuple.

## 4. Your role and responsibilities

| You own | Details |
|---|---|
| **Reviewing Bala's PRs** | Branch protection needs *your* approval for every PR Bala opens (and Bala's for yours). Check the diff, check there is a test, then use **Approve** (a comment is not an approval). Nobody approves their own work, so don't fold Bala's commits into your PR for him to approve: that is how #3 ended up merged without your approval. |
| **Your feature (Part H)** | Done: the interview kit agent, merged in PRs #5 and #6 and fixed in #9. See section 5. |
| **The merge conflict** | Done: you resolved `registry.py` and `index.html` in PR #6 (`f0b1a88`). |
| **"Break it on purpose" (Part I)** | Done by Bala in PR #8: on a new branch, a test was changed to fail, CI went red and the merge stayed blocked; the test was fixed, CI went green, and the PR was closed unmerged. |

## 5. Your feature

### Status: done

The interview kit is merged (PRs #5 and #6), and its follow-up fixes are merged in PR #9 and
live on Render: the kit goes to the interviewer, the prompt has the untrusted-resume and
protected-characteristics rules, and the icon is `clipboard-check`. Issue #4 is closed.

For reference, what PR #9 fixed:

1. **Addressed to the interviewer.** `prepare()` now uses
   `AgentContext.interviewer_of(interview)` (guarded by `interview:read`) instead of
   `cand.email`, so the questions and rubric never reach the candidate.
2. **Prompt safety.** The SYSTEM prompt now says the resume is untrusted text from an
   applicant and that protected characteristics must be ignored and never asked about.
3. **Icon.** `ASSISTANTS.interview_kit` and both buttons use `clipboard-check`.
4. **Git email.** Your later commits (`bc46c9e`, `8c89fbc`, `c0d990e`) are linked to your
   GitHub account. The older local commit `f0b1a88` still carries the placeholder email, so it
   shows as unlinked; no action needed unless you make more local commits, in which case run
   `git config user.email "202484788+Abinesh-Sundar@users.noreply.github.com"`.

The code-along is complete. The only open housekeeping is deleting the merged branches with
the approval-gated `cleanup-branches` workflow (PR #10): Bala runs it, you approve the
`branch-cleanup` deployment, and it deletes the branches.

### What the interview-kit agent does (reference, already built)

Before an interview, the interviewer gets a tailored set of questions for that candidate.

- File `app/agents/interview_kit.py`, class `InterviewKitAgent`, `name = "interview_kit"`.
- Action `prepare(ctx, interview_id)`: reads the interview, its candidate's resume and the job
  requirements, then asks the LLM for 6–8 questions (technical, plus follow-ups on resume
  claims and the screening gaps) and a short "what good answers look like" rubric.
- Saves the result as a **Draft** (`kind="interview_kit"`, `interview_id=...`) so HR reviews it.
- **Scopes:** `job:read`, `candidate:read`, `interview:read`, `draft:write`. It must **not**
  get `job:salary:read`. It reaches data through `AgentContext.interview(id)` and
  `interviewer_of(interview)`, both guarded by `interview:read`.
- Registered in `app/agents/registry.py`.
- UI (`app/static/app.js`):
  - `ASSISTANTS.interview_kit` is `{ label: "AI Interview kit", icon: "clipboard-check", does: "..." }`
    (`DRAFT_KIND` already has `interview_kit`);
  - `viewInterviews` has a `btn ai sm` button "Prepare interview kit" with
    `${icon("clipboard-check")}` on each row; it calls `POST /agents/interview_kit/prepare`
    through the existing `act(...)` helper, then `go("review")` so HR sees the draft.
- **Tests** (`tests/test_interview_kit.py`): it creates a pending draft; it never receives
  `salary_band`; an interviewer calling it gets 403 and an AgentRun row with status `denied`;
  an unknown `interview_id` gives 404.

## 6. Rules for you and your agent (non-negotiable)

1. **Never push to `main`.** Always: `git checkout main && git pull` →
   `git checkout -b feature/abinesh-<feature>` → commit → push → open a PR → Bala approves.
2. **Every feature has tests.** Run `python -m pytest -v` from the repo root before pushing.
   It must be green.
3. **Tests never call a real LLM or AgentOps.** `tests/conftest.py` forces `LLM_PROVIDER=fake`.
   If your feature parses LLM output, make `FakeLLM` return something sensible for it.
4. **Agents only go through `AgentContext`.** No `SessionLocal`, no `ctx._db` in agent code.
   New data access = a new context method that calls `self.require("<scope>")` first.
5. **Agents only draft.** No sending email, no changing a candidate's final decision status.
6. **No secrets in the repo.** Keys live in `.env` (git-ignored) locally and in Render's
   environment in production. Never commit `.env`. Don't paste keys into PR descriptions.
7. **Keep the job named `test`** in `ci.yml`. Branch protection depends on that name.
8. Don't add a database server, background workers or a second container. It's one container.
9. Keep using the fake resumes in `sample_data/`, never real people's resumes.
10. **UI rules:** no emoji anywhere. Icons only from Lucide (`icon("name")`, names at
    lucide.dev/icons). Anything an AI wrote or suggested is violet (`btn ai`, `tag ai`, `ai-note`);
    amber means "waiting on you"; green means done. Copy is plain, sentence case, written for an
    HR person, not a developer ("Prepare interview kit", not "Run interview_kit.prepare").
11. Commits: no AI-tool attribution lines (no "Co-Authored-By: Claude" or similar).

## 7. Setup and daily workflow

```bash
git clone https://github.com/spike-crypto/AI-Agent-Ops-Agent-IDP.git
cd AI-Agent-Ops-Agent-IDP
python -m venv .venv
.venv\Scripts\activate                 # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
copy .env.example .env                 # ask Bala for the OpenRouter key, or leave it empty (FakeLLM)
python -m pytest -v
uvicorn app.main:app --reload --env-file .env
```

Open http://localhost:8000, sign in with `ADMIN_EMAIL` / `ADMIN_PASSWORD` from your `.env`,
create a job, upload `sample_data/*.txt`, and click through the agents. API docs: `/docs`.

**Resolving the expected merge conflict:**

```bash
git checkout main && git pull
git checkout feature/abinesh-<feature>
git merge main
# In the conflicted file keep BOTH sides, delete the <<<<<<< ======= >>>>>>> lines
python -m pytest -v
git add <file>
git commit -m "Merge main and resolve conflict"
git push
```

CI runs again on the PR. Once it is green and approved, merge. Every merge auto-deploys to
Render, and the Deploy run's smoke test proves the new commit is live.

## 8. Done when (from the code-along)

- `main` is protected: PR required, 1 approval, green `test` check, no bypass.
- Both people's features were merged through reviewed PRs.
- The merge conflict was resolved.
- Every merge produced a green Deploy run ending with the smoke test.
- `APP_URL/health` shows the latest commit ID, and every feature works on the live URL.

Status: all met. The only caveat is the missing formal approval on PR #3; see
`PROJECT_STATE.md` §5.

## 9. If something breaks

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: app` in tests | Run `python -m pytest` from the repo root |
| Can't push | Accept the collaborator invite email from GitHub first |
| PR can't merge | It needs a green `test` check **and** the other person's approval |
| Deploy job fails at the hook | Secret `RENDER_DEPLOY_HOOK_URL` missing or wrong (Bala sets it) |
| Smoke test times out | Check Render's deploy logs; check `APP_URL` has `https://` and no trailing slash. Free instances can take a minute to wake up |
| Every merge deploys twice | Render auto-deploy got turned on: set it back to Off |
| Agent returns 502 | The free OpenRouter model is rate-limited or down: wait, or add a fallback model to `OPENROUTER_MODELS` |
