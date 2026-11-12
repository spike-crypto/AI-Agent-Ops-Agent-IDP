from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import SCOPES, AgentPermissionError
from app.agents.registry import AGENTS
from app.agents.runner import run_agent
from app.db import get_db
from app.deps import get_current_user, require_hr
from app.jobqueue import enqueue, job_out
from app.llm import LLM, LLMError, get_llm_optional
from app.logging_config import get_logger
from app.models import AgentJob, AgentRun, User

router = APIRouter(tags=["agents"])
log = get_logger("hr_agents.agents")


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
        db: Session = Depends(get_db), user: User = Depends(get_current_user),
        llm: LLM | None = Depends(get_llm_optional)):
    if llm is None:
        raise HTTPException(503, "The AI provider is not configured (OPENROUTER_API_KEY is missing)")
    try:
        return run_agent(db, user, llm, agent_name, action, params)
    except AgentPermissionError as exc:
        raise HTTPException(403, str(exc))
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except LLMError as exc:
        raise HTTPException(502, f"The AI model failed: {exc}")


@router.post("/agent-jobs", status_code=202)
def create_job(body: dict = Body(...), db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    """Queue a long agent run. Poll GET /agent-jobs/{id} for progress and the result."""
    agent = str(body.get("agent", ""))
    action = str(body.get("action", ""))
    params = body.get("params") or {}
    if not isinstance(params, dict):
        raise HTTPException(400, "params must be an object")
    try:
        job = enqueue(db, user, agent, action, params)
    except AgentPermissionError as exc:
        raise HTTPException(403, str(exc))
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return job_out(job)


@router.get("/agent-jobs")
def list_jobs(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    q = select(AgentJob).order_by(AgentJob.id.desc()).limit(limit)
    if user.role != "hr_admin":
        q = q.where(AgentJob.user_id == user.id)
    return [job_out(j) for j in db.scalars(q)]


@router.get("/agent-jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    job = db.get(AgentJob, job_id)
    if job is None or (job.user_id != user.id and user.role != "hr_admin"):
        raise HTTPException(404, "Job not found")
    return job_out(job)


@router.post("/agent-jobs/{job_id}/cancel")
def cancel_job(job_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    job = db.get(AgentJob, job_id)
    if job is None or (job.user_id != user.id and user.role != "hr_admin"):
        raise HTTPException(404, "Job not found")
    if job.status in ("done", "error", "cancelled"):
        return job_out(job)
    # Queued: cancel now. Running: the worker sees the status and stops after the current resume.
    from sqlalchemy import update

    db.execute(update(AgentJob).where(AgentJob.id == job_id).values(status="cancelled"))
    db.commit()
    log.info("job.cancel_requested", extra={"job_id": job_id, "user": user.email})
    db.refresh(job)
    return job_out(job)


@router.get("/agent-runs")
def agent_runs(limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db),
               _: User = Depends(require_hr)):
    q = (select(AgentRun, User.email).join(User, User.id == AgentRun.user_id)
         .order_by(AgentRun.id.desc()).limit(limit))
    return [
        {"id": r.id, "agent": r.agent, "action": r.action, "user": email, "status": r.status,
         "scopes_used": r.scopes_used, "summary": r.summary, "error": r.error,
         "duration_ms": r.duration_ms, "created_at": r.created_at.isoformat(timespec="seconds")}
        for r, email in db.execute(q)
    ]
