from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, require_hr, require_role
from app.models import Candidate, Feedback, Interview, Job, User

router = APIRouter(tags=["interviews & feedback"])


class ConfirmIn(BaseModel):
    slot: str


class FeedbackIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    notes: str = Field(min_length=1)


def _rows(db: Session, where=None) -> list[dict]:
    q = (
        select(Interview, Candidate, User, Job, Feedback)
        .join(Candidate, Candidate.id == Interview.candidate_id)
        .join(User, User.id == Interview.interviewer_id)
        .join(Job, Job.id == Candidate.job_id)
        .outerjoin(Feedback, Feedback.interview_id == Interview.id)
        .order_by(Interview.id.desc())
    )
    if where is not None:
        q = q.where(where)
    out = []
    for i, c, u, j, f in db.execute(q):
        out.append({
            "id": i.id, "status": i.status, "proposed_slots": i.proposed_slots,
            "scheduled_at": i.scheduled_at.isoformat(timespec="minutes") if i.scheduled_at else None,
            "candidate": {"id": c.id, "name": c.name}, "job": {"id": j.id, "title": j.title},
            "interviewer": {"id": u.id, "name": u.name},
            "feedback": {"rating": f.rating, "notes": f.notes} if f else None,
        })
    return out


@router.get("/interviews")
def all_interviews(db: Session = Depends(get_db), _: User = Depends(require_hr)):
    return _rows(db)


@router.get("/my/interviews")
def my_interviews(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _rows(db, Interview.interviewer_id == user.id)


@router.post("/interviews/{interview_id}/confirm")
def confirm_slot(interview_id: int, body: ConfirmIn, db: Session = Depends(get_db), _: User = Depends(require_hr)):
    """HR confirms the slot the candidate picked."""
    interview = db.get(Interview, interview_id)
    if interview is None:
        raise HTTPException(404, "Interview not found")
    if interview.status == "done":
        raise HTTPException(409, "This interview is already finished")
    if body.slot not in interview.proposed_slots:
        raise HTTPException(400, "That slot was not one of the proposed slots")
    try:
        interview.scheduled_at = datetime.fromisoformat(body.slot)
    except ValueError:
        raise HTTPException(400, "Slots must be ISO date-times, e.g. 2026-11-20T10:00")
    interview.status = "scheduled"
    db.commit()
    return {"id": interview.id, "status": interview.status, "scheduled_at": body.slot}


@router.post("/interviews/{interview_id}/feedback", status_code=201)
def submit_feedback(interview_id: int, body: FeedbackIn, db: Session = Depends(get_db),
                    user: User = Depends(require_role("hr_admin", "interviewer"))):
    interview = db.get(Interview, interview_id)
    # Only the assigned interviewer can give feedback on an interview.
    if interview is None or interview.interviewer_id != user.id:
        raise HTTPException(404, "Interview not found")
    if interview.status == "proposed":
        raise HTTPException(400, "This interview has not been scheduled yet")
    if db.scalar(select(Feedback.id).where(Feedback.interview_id == interview_id)):
        raise HTTPException(409, "Feedback already submitted")
    db.add(Feedback(interview_id=interview_id, interviewer_id=user.id,
                    rating=body.rating, notes=body.notes))
    interview.status = "done"
    try:
        db.commit()
    except IntegrityError:
        # Two submissions at the same moment: the unique index is the real guard.
        db.rollback()
        raise HTTPException(409, "Feedback already submitted")
    return {"ok": True}
