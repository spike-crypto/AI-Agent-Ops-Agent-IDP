"""What the Copilot is allowed to do: a fixed catalog of tools in three kinds.

- read:     the Copilot's own lookups. Checked against the read scopes on its token. Compact
            rows only: resume text never leaves here, and model-written summaries (which come
            from applicant text) are marked untrusted.
- delegate: hand one step to the agent that owns it. Each call exchanges the Copilot's token
            for a single-use token for that agent (see app/identity.py) and runs the agent with it.
            These only score, summarise or draft into the review queue.
- propose:  never run. They return an item for an action card; HR must approve it, then
            `apply_item` runs it through a token exactly like a delegate tool.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import identity
from app.agents.base import AgentPermissionError
from app.llm import LLM, LLMError
from app.logging_config import get_logger
from app.models import CANDIDATE_STATUSES, Candidate, Draft, Feedback, Interview, Job, User

log = get_logger("hr_agents.copilot_tools")

STAGE_LABELS = {
    "new": "New", "screened": "Screened", "shortlisted": "Shortlisted",
    "interviewing": "Interviewing", "offered": "Offer sent", "rejected": "Not moving forward",
}
MAX_TOP = 25
# ~40 s per resume on free models, 3 at a time: 4 keeps one screening step well inside a turn.
CHAT_SCREEN_LIMIT = 4


@dataclass(frozen=True)
class Tool:
    name: str
    kind: str                       # read | delegate | propose
    description: str
    params: dict[str, str]          # name -> "int" | "str" | "list[int]" | "list[str]"; "?" = optional
    scopes: frozenset[str]          # read: needed on the Copilot token; delegate/propose: for the target
    agent: str = ""                 # delegate/propose: the agent that does the work
    action: str = ""


@dataclass
class ToolResult:
    ok: bool
    summary: str
    data: dict = field(default_factory=dict)
    proposal_item: dict | None = None
    grant: dict | None = None


def _t(name, kind, description, params, scopes, agent="", action="") -> Tool:
    return Tool(name, kind, description, params, frozenset(scopes), agent, action)


_SCREEN = {"job:read", "candidate:read", "candidate:write"}
_WRITE = {"job:read", "candidate:read", "draft:write"}
_OFFER = _WRITE | {"job:salary:read"}
_SCHEDULE = {"job:read", "candidate:read", "interview:write", "draft:write"}
_STAGE = {"candidate:read", "candidate:stage:write"}

TOOLS: dict[str, Tool] = {t.name: t for t in [
    _t("list_jobs", "read", "List the open jobs with how many candidates are at each stage.",
       {}, {"job:read"}),
    _t("find_job", "read", "Find jobs whose title contains the given words.",
       {"name": "str"}, {"job:read"}),
    _t("list_candidates", "read",
       "List a job's candidates, best screening score first. Optional stage filter and top N.",
       {"job_id": "int", "stage": "str?", "top": "int?"}, {"job:read", "candidate:read"}),
    _t("get_candidate", "read", "One candidate's stage, score, screening strengths and gaps, and interviews.",
       {"candidate_id": "int"}, {"candidate:read"}),
    _t("list_interviews", "read",
       "Interviews with their status. status may be proposed, scheduled, done or missing_feedback.",
       {"status": "str?"}, {"interview:read"}),
    _t("pending_drafts", "read", "Drafts waiting for HR in the review queue (titles only).",
       {}, {"draft:read"}),
    _t("screen", "delegate",
       f"Score a job's unscreened resumes against its requirements (at most {CHAT_SCREEN_LIMIT} per request).",
       {"job_id": "int"}, _SCREEN, "screening", "screen"),
    _t("compare", "delegate", "Compare 2 to 4 candidates of one job side by side.",
       {"candidate_ids": "list[int]"}, {"job:read", "candidate:read"}, "comparison", "compare"),
    _t("summarize_feedback", "delegate", "Summarise the interview feedback for one candidate.",
       {"candidate_id": "int"}, {"candidate:read", "candidate:write", "feedback:read"}, "feedback", "summarize"),
    _t("decision_memo", "delegate", "Draft a hiring decision memo for one candidate (goes to the review queue).",
       {"candidate_id": "int"}, {"job:read", "candidate:read", "feedback:read", "draft:write"},
       "feedback", "synthesize"),
    _t("interview_kit", "delegate", "Draft interview questions and a rubric for one interview (review queue).",
       {"interview_id": "int"}, {"job:read", "candidate:read", "interview:read", "draft:write"},
       "interview_kit", "prepare"),
    _t("propose_stage_change", "propose",
       "Propose moving a candidate to a stage (new, screened, shortlisted, interviewing, offered, rejected). "
       "HR must approve.", {"candidate_id": "int", "stage": "str"}, _STAGE, "recruiter", "set_stage"),
    _t("propose_rejection_email", "propose", "Propose drafting a kind rejection email. HR must approve.",
       {"candidate_id": "int"}, _WRITE, "writing", "rejection"),
    _t("propose_offer_draft", "propose", "Propose drafting an offer letter. HR must approve.",
       {"candidate_id": "int"}, _OFFER, "writing", "offer"),
    _t("propose_interview", "propose",
       "Propose an interview with times to offer (ISO date-times). HR must approve.",
       {"candidate_id": "int", "interviewer_id": "int", "slots": "list[str]"}, _SCHEDULE,
       "scheduling", "propose"),
]}

READ_SCOPES = frozenset({"job:read", "candidate:read", "interview:read", "feedback:read", "draft:read"})

# Everything the Copilot may hold or pass on. Its own code only ever reads; writes happen
# inside the agents it delegates to, each under a narrower token.
COPILOT_SCOPES = frozenset().union(*(t.scopes for t in TOOLS.values()), {"draft:read"})


class ToolInputError(ValueError):
    pass


def _coerce(tool: Tool, args: dict) -> dict:
    if not isinstance(args, dict):
        raise ToolInputError("Arguments must be an object")
    out: dict[str, Any] = {}
    for name, kind in tool.params.items():
        optional = kind.endswith("?")
        kind = kind.rstrip("?")
        if name not in args or args[name] in (None, ""):
            if optional:
                continue
            raise ToolInputError(f"{tool.name} needs '{name}'")
        value = args[name]
        try:
            if kind == "int":
                out[name] = int(value)
            elif kind == "str":
                out[name] = str(value)[:200]
            elif kind.startswith("list["):
                if not isinstance(value, (list, tuple)):
                    raise ToolInputError(f"'{name}' for {tool.name} must be a list")
                out[name] = ([int(v) for v in value][:10] if kind == "list[int]"
                             else [str(v)[:40] for v in value][:5])
        except (TypeError, ValueError):
            raise ToolInputError(f"'{name}' for {tool.name} must be {kind}") from None
    return out


def _untrusted(text: str | None) -> dict:
    """Model-written text derived from applicant input. Shown to the planner as data, not orders."""
    return {"untrusted_text": (text or "")[:300]}


def _check_scope(db: Session, copilot_token: str, scopes: frozenset[str]) -> None:
    grant = identity.verify(db, copilot_token, identity.agent_id("copilot"), consume=False)
    missing = sorted(scopes - grant.scopes)
    if missing:
        raise AgentPermissionError(f"The Copilot's token does not allow {', '.join(missing)}")


# ---- read tools -----------------------------------------------------------------------------

def _stage_counts(db: Session) -> dict[int, dict[str, int]]:
    rows = db.execute(select(Candidate.job_id, Candidate.status, func.count()).group_by(
        Candidate.job_id, Candidate.status)).all()
    counts: dict[int, dict[str, int]] = {}
    for job_id, status, n in rows:
        counts.setdefault(job_id, {})[status] = n
    return counts


def _job_row(job: Job, counts: dict) -> dict:
    return {"id": job.id, "title": job.title, "department": job.department,
            "stages": counts.get(job.id, {})}


def list_jobs(db: Session) -> ToolResult:
    counts = _stage_counts(db)
    jobs = [_job_row(j, counts) for j in db.scalars(select(Job).order_by(Job.id))]
    return ToolResult(True, f"Found {len(jobs)} jobs", {"jobs": jobs})


def find_job(db: Session, name: str) -> ToolResult:
    words = [w for w in name.lower().split() if w]
    counts = _stage_counts(db)
    jobs = [_job_row(j, counts) for j in db.scalars(select(Job).order_by(Job.id))
            if all(w in j.title.lower() for w in words)][:5]
    if not jobs:
        return ToolResult(True, f"No job title contains '{name}'", {"jobs": []})
    return ToolResult(True, f"Found {len(jobs)} matching job(s): " + ", ".join(j["title"] for j in jobs),
                      {"jobs": jobs})


def _cand_row(c: Candidate) -> dict:
    row = {"id": c.id, "name": c.name, "job_id": c.job_id, "stage": c.status,
           "stage_label": STAGE_LABELS.get(c.status, c.status), "score": c.score}
    if c.screening:
        row["screening_summary"] = _untrusted(c.screening.get("summary"))
    return row


def list_candidates(db: Session, job_id: int, stage: str | None = None, top: int | None = None) -> ToolResult:
    job = db.get(Job, job_id)
    if job is None:
        raise LookupError(f"Job {job_id} not found")
    q = select(Candidate).where(Candidate.job_id == job_id)
    if stage:
        q = q.where(Candidate.status == stage)
    cands = sorted(db.scalars(q), key=lambda c: (c.score is None, -(c.score or 0), c.id))
    cands = cands[: max(1, min(top or MAX_TOP, MAX_TOP))]
    unscreened = sum(c.score is None for c in cands)
    return ToolResult(True, f"{len(cands)} candidate(s) for {job.title}"
                      + (f", {unscreened} not screened yet" if unscreened else ""),
                      {"job": {"id": job.id, "title": job.title}, "candidates": [_cand_row(c) for c in cands]})


def get_candidate(db: Session, candidate_id: int) -> ToolResult:
    c = db.get(Candidate, candidate_id)
    if c is None:
        raise LookupError(f"Candidate {candidate_id} not found")
    row = _cand_row(c)
    if c.screening:
        row["strengths"] = _untrusted(", ".join(c.screening.get("strengths") or []))
        row["gaps"] = _untrusted(", ".join(c.screening.get("gaps") or []))
    row["interviews"] = [
        {"id": i.id, "status": i.status, "when": i.scheduled_at.isoformat(timespec="minutes") if i.scheduled_at else None}
        for i in db.scalars(select(Interview).where(Interview.candidate_id == c.id))]
    return ToolResult(True, f"{c.name}: {row['stage_label']}, score {c.score if c.score is not None else 'not screened'}",
                      {"candidate": row})


def list_interviews(db: Session, status: str | None = None) -> ToolResult:
    from app.models import utcnow

    rows = []
    q = (select(Interview, Candidate, User).join(Candidate, Candidate.id == Interview.candidate_id)
         .join(User, User.id == Interview.interviewer_id).order_by(Interview.id))
    has_feedback = set(db.scalars(select(Feedback.interview_id)))
    for i, c, u in db.execute(q):
        missing = i.status == "scheduled" and i.scheduled_at and i.scheduled_at <= utcnow() and i.id not in has_feedback
        if status == "missing_feedback" and not missing:
            continue
        if status and status != "missing_feedback" and i.status != status:
            continue
        rows.append({"id": i.id, "candidate": c.name, "candidate_id": c.id, "interviewer": u.name,
                     "status": i.status, "feedback_missing": bool(missing),
                     "when": i.scheduled_at.isoformat(timespec="minutes") if i.scheduled_at else None})
    return ToolResult(True, f"{len(rows)} interview(s)", {"interviews": rows[:MAX_TOP]})


def pending_drafts(db: Session) -> ToolResult:
    drafts = list(db.scalars(select(Draft).where(Draft.status == "pending").order_by(Draft.id)))
    return ToolResult(True, f"{len(drafts)} draft(s) waiting for review",
                      {"drafts": [{"id": d.id, "kind": d.kind, "subject": d.subject, "agent": d.agent}
                                  for d in drafts[:MAX_TOP]]})


_READERS = {"list_jobs": list_jobs, "find_job": find_job, "list_candidates": list_candidates,
            "get_candidate": get_candidate, "list_interviews": list_interviews,
            "pending_drafts": pending_drafts}


# ---- delegate and propose -------------------------------------------------------------------

def _compact(agent: str, result: dict) -> dict:
    """What the planner needs from an agent's result, kept small."""
    out: dict[str, Any] = {"summary": str(result.get("summary", ""))}
    if agent == "screening":
        out["ranked"] = [{"id": r["id"], "name": r["name"], "score": r["score"]} for r in result.get("ranked", [])[:10]]
        out["could_not_screen"] = len(result.get("failed", []))
    elif agent == "comparison":
        out["overview"] = _untrusted(result.get("overview"))
        out["candidates"] = [{"id": c["id"], "name": c["name"], "score": c.get("score"),
                              "strengths": _untrusted(", ".join(c.get("strengths") or [])),
                              "risks": _untrusted(", ".join(c.get("risks") or []))}
                             for c in result.get("candidates", [])]
        out["open_questions"] = _untrusted("; ".join(result.get("open_questions") or []))
    elif "draft" in result:
        out["draft"] = {"id": result["draft"]["id"], "kind": result["draft"]["kind"],
                        "subject": result["draft"]["subject"]}
    for key in ("average_rating", "interview_id", "candidate_id", "from", "to"):
        if key in result:
            out[key] = result[key]
    return out


def _delegate(db: Session, user: User, llm: LLM, copilot_token: str, agent: str, action: str,
              scopes: frozenset[str], params: dict, session_id: str | None) -> ToolResult:
    from app.agents.runner import run_agent  # here, not at the top: the registry imports this module

    token = identity.exchange(db, copilot_token, agent, scopes)
    grant = identity.verify(db, token, identity.agent_id(agent), consume=False)
    grant_info = {"jti": grant.jti, "audience": grant.audience, "actors": list(grant.actors),
                  "scopes": sorted(grant.scopes)}
    result = run_agent(db, user, llm, agent, action, params, delegation=token, session_id=session_id)
    data = _compact(agent, result)
    return ToolResult(True, data["summary"], data, grant=grant_info)


def _propose(db: Session, tool: Tool, args: dict) -> ToolResult:
    cand = db.get(Candidate, args["candidate_id"])
    if cand is None:
        raise LookupError(f"Candidate {args['candidate_id']} not found")
    if tool.name == "propose_stage_change":
        if args["stage"] not in CANDIDATE_STATUSES:
            raise ToolInputError(f"Unknown stage '{args['stage']}'. Use one of: {', '.join(CANDIDATE_STATUSES)}")
        label = f"Move {cand.name} to {STAGE_LABELS[args['stage']]}"
    elif tool.name == "propose_rejection_email":
        label = f"Draft a rejection email to {cand.name}"
    elif tool.name == "propose_offer_draft":
        label = f"Draft an offer letter for {cand.name}"
    else:
        interviewer = db.get(User, args["interviewer_id"])
        if interviewer is None or not interviewer.is_active:
            raise LookupError(f"Interviewer {args['interviewer_id']} not found")
        if not args["slots"]:
            raise ToolInputError("Offer at least one interview time")
        when = []
        for slot in args["slots"]:
            try:
                dt = datetime.fromisoformat(slot)
            except ValueError:
                raise ToolInputError(f"'{slot}' is not a date and time (use 2026-11-20T10:00)") from None
            when.append(f"{dt.day} {dt:%b %Y, %H:%M}")
        label = f"Propose an interview for {cand.name} with {interviewer.name} at " + " or ".join(when)
    item = {"tool": tool.name, "agent": tool.agent, "action": tool.action, "params": args, "label": label}
    return ToolResult(True, f"Prepared for HR approval: {label}", {"label": label}, proposal_item=item)


def run_tool(db: Session, user: User, llm: LLM, copilot_token: str, name: str, args: dict,
             session_id: str | None = None) -> ToolResult:
    """Run one tool chosen by the planner. Never raises for a bad call: the planner reads the summary."""
    tool = TOOLS.get(name)
    if tool is None:
        return ToolResult(False, f"There is no tool called '{name}'. Use one from the list.")
    try:
        clean = _coerce(tool, args or {})
        if tool.kind == "read":
            _check_scope(db, copilot_token, tool.scopes)
            return _READERS[name](db, **clean)
        if tool.kind == "propose":
            _check_scope(db, copilot_token, frozenset({"candidate:read"}))
            return _propose(db, tool, clean)
        params = {**clean, "limit": CHAT_SCREEN_LIMIT} if name == "screen" else clean
        return _delegate(db, user, llm, copilot_token, tool.agent, tool.action, tool.scopes, params, session_id)
    except (ToolInputError, LookupError, ValueError, AgentPermissionError, LLMError) as exc:
        db.rollback()
        log.info("copilot.tool_failed", extra={"tool": name, "error": str(exc)[:200]})
        return ToolResult(False, f"{name} failed: {exc}")
    except Exception as exc:  # an agent crashed: report it as a failed step, never end the turn
        db.rollback()
        log.warning("copilot.tool_crashed", extra={"tool": name, "error": str(exc)[:200]})
        return ToolResult(False, f"{name} failed unexpectedly: {exc}")


def apply_item(db: Session, user: User, llm: LLM, copilot_token: str, item: dict,
               session_id: str | None = None) -> ToolResult:
    """Run one HR-approved action-card line, through an exchanged token like any delegated step."""
    tool = TOOLS.get(item.get("tool", ""))
    if tool is None or tool.kind != "propose":
        return ToolResult(False, "This approved item is not a known action")
    try:
        clean = _coerce(tool, item.get("params") or {})
        return _delegate(db, user, llm, copilot_token, tool.agent, tool.action, tool.scopes, clean, session_id)
    except Exception as exc:  # report every failure as a failed line; the card must still finish
        db.rollback()
        return ToolResult(False, f"{item.get('label', tool.name)} failed: {exc}")
