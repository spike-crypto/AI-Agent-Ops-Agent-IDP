"""The single entry point for running an agent: checks roles, builds the scoped
context, traces the run in AgentOps, and writes an AgentRun audit row whatever happens."""

import time

from sqlalchemy.orm import Session

from app.agents.base import ROLE_SCOPES, AgentContext, AgentPermissionError
from app.agents.registry import AGENTS
from app.llm import LLM
from app.models import AgentRun, User
from app.observability import trace


def run_agent(db: Session, user: User, llm: LLM, agent_name: str, action: str, params: dict) -> dict:
    agent = AGENTS.get(agent_name)
    if agent is None:
        raise LookupError(f"No agent named '{agent_name}'")

    started = time.perf_counter()

    def record(status: str, ctx: AgentContext | None, summary: str = "", error: str = "") -> None:
        db.add(AgentRun(
            agent=agent_name, action=action, user_id=user.id, status=status,
            scopes_used=sorted(ctx.used) if ctx else [], summary=summary, error=error[:2000],
            duration_ms=int((time.perf_counter() - started) * 1000),
        ))
        db.commit()

    if user.role not in agent.allowed_roles:
        record("denied", None, error=f"Role '{user.role}' may not run agent '{agent_name}'")
        raise AgentPermissionError(f"Role '{user.role}' may not run agent '{agent_name}'")

    granted = agent.scopes & ROLE_SCOPES.get(user.role, frozenset())
    ctx = AgentContext(db, user, llm, agent_name, granted)
    try:
        with trace(agent_name, action, user.email):
            result = agent.handle(ctx, action, params)
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
        raise

    db.commit()
    record("success", ctx, summary=str(result.get("summary", "")))
    return result
