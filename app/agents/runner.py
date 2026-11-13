"""The single entry point for running an agent: checks roles, builds the scoped
context, traces the run in Langfuse, and writes an AgentRun audit row whatever happens."""

import time
from typing import Callable

from sqlalchemy.orm import Session

from app.agents.base import ROLE_SCOPES, AgentContext, AgentPermissionError
from app.agents.registry import AGENTS
from app.llm import LLM
from app.logging_config import get_logger
from app.models import AgentRun, Candidate, Interview, User
from app.observability import trace

log = get_logger("hr_agents.runner")


def session_for(db: Session, params: dict) -> str | None:
    """The Langfuse session for a run: one per job opening, so every agent run about the
    same job (screening, comparing, invites, kits, offers) is grouped together.
    Returns None for runs that span jobs, such as chasing all missing feedback."""
    try:
        job_id = params.get("job_id")
        if job_id is None:
            candidate_id = params.get("candidate_id")
            if candidate_id is None and params.get("candidate_ids"):
                candidate_id = params["candidate_ids"][0]
            if candidate_id is None and params.get("interview_id") is not None:
                interview = db.get(Interview, int(params["interview_id"]))
                candidate_id = interview.candidate_id if interview else None
            if candidate_id is not None:
                candidate = db.get(Candidate, int(candidate_id))
                job_id = candidate.job_id if candidate else None
        return f"job-{int(job_id)}" if job_id is not None else None
    except (TypeError, ValueError, AttributeError):
        return None  # odd parameters are the agent's problem to report, not tracing's


def run_agent(
    db: Session,
    user: User,
    llm: LLM,
    agent_name: str,
    action: str,
    params: dict,
    progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    agent = AGENTS.get(agent_name)
    if agent is None:
        raise LookupError(f"No agent named '{agent_name}'")

    started = time.perf_counter()

    def record(status: str, ctx: AgentContext | None, summary: str = "", error: str = "") -> None:
        duration_ms = int((time.perf_counter() - started) * 1000)
        db.add(AgentRun(
            agent=agent_name, action=action, user_id=user.id, status=status,
            scopes_used=sorted(ctx.used) if ctx else [], summary=summary, error=error[:2000],
            duration_ms=duration_ms,
        ))
        db.commit()
        log.info(
            "agent.run",
            extra={"agent": agent_name, "action": action, "status": status, "user": user.email,
                   "duration_ms": duration_ms, "scopes": sorted(ctx.used) if ctx else [],
                   "error": error[:300]},
        )

    if user.role not in agent.allowed_roles:
        record("denied", None, error=f"Role '{user.role}' may not run agent '{agent_name}'")
        raise AgentPermissionError(f"Role '{user.role}' may not run agent '{agent_name}'")

    granted = agent.scopes & ROLE_SCOPES.get(user.role, frozenset())
    ctx = AgentContext(db, user, llm, agent_name, granted, progress=progress, should_stop=should_stop)
    try:
        with trace(agent_name, action, user.email, params, session_for(db, params)) as run:
            result = agent.handle(ctx, action, params)
            if isinstance(result, dict):
                run.record(output=str(result.get("summary", "")),
                           metadata={"scopes_used": sorted(ctx.used), "role": user.role})
    except AgentPermissionError as exc:
        db.rollback()
        record("denied", ctx, error=str(exc))
        raise
    except TypeError as exc:  # wrong or missing parameters for the action
        db.rollback()
        record("error", ctx, error=str(exc))
        raise ValueError(f"Bad parameters for {agent_name}.{action}: {exc}") from exc
    except Exception as exc:
        db.rollback()
        record("error", ctx, error=str(exc))
        log.error("agent.failed", exc_info=True,
                  extra={"agent": agent_name, "action": action, "user": user.email})
        raise

    db.commit()
    if not isinstance(result, dict):
        record("error", ctx, error=f"Agent returned {type(result).__name__}, expected dict")
        raise ValueError(f"Agent {agent_name}.{action} returned an invalid result")
    record("success", ctx, summary=str(result.get("summary", "")))
    return result
