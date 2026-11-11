from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import SCOPES, AgentPermissionError
from app.agents.registry import AGENTS
from app.agents.runner import run_agent
from app.db import get_db
from app.deps import get_current_user, require_hr
from app.llm import LLMError, get_llm
from app.models import AgentRun, User

router = APIRouter(tags=["agents"])


@router.get("/agents")
def list_agents(_: User = Depends(get_current_user)):
    return [
        {"name": a.name, "description": a.description, "actions": list(a.actions),
         "allowed_roles": sorted(a.allowed_roles),
         "scopes": [{"scope": s, "meaning": SCOPES[s]} for s in sorted(a.scopes)]}
        for a in AGENTS.values()
    ]


@router.post("/agents/{agent_name}/{action}")
def run(agent_name: str, action: str, params: dict = Body(default_factory=dict),
        db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        return run_agent(db, user, get_llm(), agent_name, action, params)
    except AgentPermissionError as exc:
        raise HTTPException(403, str(exc))
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except LLMError as exc:
        raise HTTPException(502, f"The AI model failed: {exc}")


@router.get("/agent-runs")
def agent_runs(limit: int = 100, db: Session = Depends(get_db), _: User = Depends(require_hr)):
    q = (select(AgentRun, User.email).join(User, User.id == AgentRun.user_id)
         .order_by(AgentRun.id.desc()).limit(min(limit, 500)))
    return [
        {"id": r.id, "agent": r.agent, "action": r.action, "user": email, "status": r.status,
         "scopes_used": r.scopes_used, "summary": r.summary, "error": r.error,
         "duration_ms": r.duration_ms, "created_at": r.created_at.isoformat(timespec="seconds")}
        for r, email in db.execute(q)
    ]
