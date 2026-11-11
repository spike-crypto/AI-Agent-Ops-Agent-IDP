"""One call that tells the HR person what needs them right now (the Today view)."""

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_hr
from app.models import CANDIDATE_STATUSES, Candidate, Draft, Feedback, Interview, Job, User, utcnow

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), _: User = Depends(require_hr)):
    now = utcnow()

    stage_rows = db.execute(
        select(Candidate.job_id, Candidate.status, func.count()).group_by(Candidate.job_id, Candidate.status)
    ).all()
    stages: dict[int, dict[str, int]] = {}
    for job_id, status, n in stage_rows:
        stages.setdefault(job_id, {s: 0 for s in CANDIDATE_STATUSES})[status] = n

    jobs = [
        {"id": j.id, "title": j.title, "department": j.department, "status": j.status,
         "has_description": bool(j.description),
         "stages": stages.get(j.id, {s: 0 for s in CANDIDATE_STATUSES})}
        for j in db.scalars(select(Job).order_by(Job.id.desc()))
    ]

    to_confirm = db.scalar(select(func.count()).select_from(Interview).where(Interview.status == "proposed"))
    feedback_missing = db.scalar(
        select(func.count()).select_from(Interview)
        .outerjoin(Feedback, Feedback.interview_id == Interview.id)
        .where(Interview.status == "scheduled", Interview.scheduled_at <= now, Feedback.id.is_(None))
    )
    upcoming = db.execute(
        select(Interview, Candidate, User)
        .join(Candidate, Candidate.id == Interview.candidate_id)
        .join(User, User.id == Interview.interviewer_id)
        .where(Interview.status == "scheduled", Interview.scheduled_at > now,
               Interview.scheduled_at <= now + timedelta(days=7))
        .order_by(Interview.scheduled_at)
        .limit(8)
    ).all()

    return {
        "needs_you": {
            "drafts_to_review": db.scalar(select(func.count()).select_from(Draft).where(Draft.status == "pending")),
            "resumes_to_screen": sum(s["new"] for s in stages.values()),
            "interviews_to_confirm": to_confirm,
            "feedback_missing": feedback_missing,
        },
        "upcoming": [
            {"interview_id": i.id, "when": i.scheduled_at.isoformat(timespec="minutes"),
             "candidate": {"id": c.id, "name": c.name}, "interviewer": u.name}
            for i, c, u in upcoming
        ],
        "jobs": jobs,
        "setup": {
            "has_job": bool(jobs),
            "has_candidates": bool(stages),
            "has_screened": any(s["screened"] + s["shortlisted"] + s["interviewing"] + s["offered"] for s in stages.values()),
            "has_interviewer": bool(db.scalar(select(User.id).where(User.role == "interviewer", User.is_active).limit(1))),
            "has_interview": bool(db.scalar(select(Interview.id).limit(1))),
        },
    }
