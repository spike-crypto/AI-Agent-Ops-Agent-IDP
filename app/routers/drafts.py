from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_hr
from app.models import Draft, Job, User, utcnow

router = APIRouter(tags=["drafts (human review)"])


class ApproveIn(BaseModel):
    content: str | None = None  # HR's edited version, if they changed it
    subject: str | None = None


def draft_out(d: Draft) -> dict:
    return {"id": d.id, "kind": d.kind, "agent": d.agent, "subject": d.subject, "recipient": d.recipient,
            "content": d.content, "status": d.status, "job_id": d.job_id, "candidate_id": d.candidate_id,
            "interview_id": d.interview_id, "created_at": d.created_at.isoformat(timespec="seconds")}


@router.get("/drafts")
def list_drafts(status: str | None = "pending", db: Session = Depends(get_db), _: User = Depends(require_hr)):
    q = select(Draft).order_by(Draft.id.desc())
    if status:
        q = q.where(Draft.status == status)
    return [draft_out(d) for d in db.scalars(q)]


def _pending_or_error(db: Session, draft_id: int) -> Draft:
    draft = db.get(Draft, draft_id)
    if draft is None:
        raise HTTPException(404, "Draft not found")
    if draft.status != "pending":
        raise HTTPException(409, f"Draft is already {draft.status}")
    return draft


@router.post("/drafts/{draft_id}/approve")
def approve(draft_id: int, body: ApproveIn, db: Session = Depends(get_db), user: User = Depends(require_hr)):
    """Approving marks it ready to send. v1 never sends email itself: HR copies and sends it."""
    draft = _pending_or_error(db, draft_id)
    if body.content is not None:
        draft.content = body.content
    if body.subject is not None:
        draft.subject = body.subject
    draft.status = "approved"
    draft.reviewed_by = user.id
    draft.reviewed_at = utcnow()
    if draft.kind == "job_description" and draft.job_id:
        job = db.get(Job, draft.job_id)
        if job:
            job.description = draft.content
    db.commit()
    return draft_out(draft)


@router.post("/drafts/{draft_id}/discard")
def discard(draft_id: int, db: Session = Depends(get_db), user: User = Depends(require_hr)):
    draft = _pending_or_error(db, draft_id)
    draft.status = "discarded"
    draft.reviewed_by = user.id
    draft.reviewed_at = utcnow()
    db.commit()
    return draft_out(draft)
