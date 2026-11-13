"""DB-backed agent job queue, processed by one in-process worker thread.

The request that starts a long run (screening 40 resumes) returns immediately; the
worker does the model calls and writes progress to the AgentJob row, which the UI polls.
On Postgres the claim uses FOR UPDATE SKIP LOCKED, so several workers/instances are safe.
"""

import json
import threading
import time

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agents.base import AgentPermissionError
from app.agents.registry import AGENTS
from app.agents.runner import run_agent
from app.config import settings
from app.db import SessionLocal
from app.llm import LLMError, get_llm
from app.logging_config import get_logger
from app.models import AgentJob, User, utcnow

log = get_logger("hr_agents.jobs")

_stop = threading.Event()
_thread: threading.Thread | None = None


def enqueue(db: Session, user: User, agent_name: str, action: str, params: dict) -> AgentJob:
    agent = AGENTS.get(agent_name)
    if agent is None:
        raise LookupError(f"No agent named '{agent_name}'")
    if action not in agent.actions:
        raise ValueError(f"Agent '{agent_name}' has no action '{action}'")
    if user.role not in agent.allowed_roles:
        raise AgentPermissionError(f"Role '{user.role}' may not run agent '{agent_name}'")
    job = AgentJob(agent=agent_name, action=action, params=params or {}, user_id=user.id)
    db.add(job)
    db.commit()
    log.info("job.enqueued", extra={"job_id": job.id, "agent": agent_name, "action": action,
                                    "user": user.email})
    return job


def job_out(job: AgentJob) -> dict:
    return {
        "id": job.id, "agent": job.agent, "action": job.action, "status": job.status,
        "progress_current": job.progress_current, "progress_total": job.progress_total,
        "result": job.result, "error": job.error, "steps": job.steps or [],
        "created_at": job.created_at.isoformat(timespec="seconds"),
        "started_at": job.started_at.isoformat(timespec="seconds") if job.started_at else None,
        "finished_at": job.finished_at.isoformat(timespec="seconds") if job.finished_at else None,
    }


def _claim() -> int | None:
    with SessionLocal() as db:
        query = select(AgentJob.id).where(AgentJob.status == "queued").order_by(AgentJob.id)
        if db.get_bind().dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        job_id = db.scalar(query)
        if job_id is None:
            return None
        claimed = db.execute(
            update(AgentJob)
            .where(AgentJob.id == job_id, AgentJob.status == "queued")
            .values(status="running", started_at=utcnow(), attempts=AgentJob.attempts + 1)
        )
        db.commit()
        return job_id if claimed.rowcount == 1 else None


def _set_progress(job_id: int, done: int, total: int) -> None:
    with SessionLocal() as db:
        db.execute(update(AgentJob).where(AgentJob.id == job_id)
                   .values(progress_current=done, progress_total=total))
        db.commit()


def _append_step(job_id: int, step: dict) -> None:
    """One finished Copilot step, written straight away so the chat can show it live."""
    with SessionLocal() as db:
        job = db.get(AgentJob, job_id)
        if job is not None:
            job.steps = [*(job.steps or []), json.loads(json.dumps(step, default=str))]
            db.commit()


def _is_cancelled(job_id: int) -> bool:
    with SessionLocal() as db:
        return db.scalar(select(AgentJob.status).where(AgentJob.id == job_id)) == "cancelled"


def _finish(job_id: int, status: str, result: dict | None = None, error: str = "") -> None:
    with SessionLocal() as db:
        values: dict = {"status": status, "finished_at": utcnow(), "error": error[:2000]}
        if result is not None:
            # JSON columns reject unexpected types; round-trip through JSON to be safe.
            values["result"] = json.loads(json.dumps(result, default=str))
        db.execute(update(AgentJob).where(AgentJob.id == job_id).values(**values))
        db.commit()


def run_job(job_id: int) -> None:
    with SessionLocal() as db:
        job = db.get(AgentJob, job_id)
        if job is None:
            return
        user = db.get(User, job.user_id)
        if user is None or not user.is_active:
            _finish(job_id, "error", error="The user who started this job is no longer active")
            return
        try:
            llm = get_llm()
        except LLMError as exc:
            _finish(job_id, "error", error=str(exc))
            return
        started = time.perf_counter()
        try:
            result = run_agent(
                db, user, llm, job.agent, job.action, job.params or {},
                progress=lambda done, total: _set_progress(job_id, done, total),
                should_stop=lambda: _is_cancelled(job_id),
                on_step=lambda step: _append_step(job_id, step),
            )
        except Exception as exc:
            db.rollback()
            _finish(job_id, "error", error=f"{type(exc).__name__}: {exc}")
            log.warning("job.failed", extra={"job_id": job_id, "error": str(exc)[:300]})
            return
        if _is_cancelled(job_id):
            _finish(job_id, "cancelled", result=result)
        else:
            _finish(job_id, "done", result=result)
        log.info("job.finished", extra={"job_id": job_id, "agent": job.agent,
                                        "duration_ms": int((time.perf_counter() - started) * 1000)})


def run_once() -> bool:
    """Claim and run at most one job. Returns True if a job was processed."""
    job_id = _claim()
    if job_id is None:
        return False
    run_job(job_id)
    return True


def _loop() -> None:
    while not _stop.is_set():
        try:
            if not run_once():
                _stop.wait(settings.worker_poll_seconds)
        except Exception:
            log.exception("job.worker_crashed")
            _stop.wait(2)


def start_worker() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="agent-worker", daemon=True)
    _thread.start()
    log.info("job.worker_started")


def stop_worker() -> None:
    global _thread
    _stop.set()
    if _thread:
        _thread.join(timeout=5)
        _thread = None
