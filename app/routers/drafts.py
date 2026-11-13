from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import observability
from app.db import get_db
from app.deps import require_hr
from app.logging_config import get_logger
from app.models import Draft, Job, User, utcnow

router = APIRouter(tags=["drafts (human review)"])
log = get_logger("hr_agents.drafts")


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


# What HR did with a draft, as a quality score on the agent run that wrote it.
REVIEW_APPROVED, REVIEW_EDITED, REVIEW_DISCARDED = 1.0, 0.5, 0.0


def _score_review(draft: Draft, value: float, outcome: str) -> None:
    log.info("draft.reviewed", extra={"draft": draft.id, "kind": draft.kind, "agent": draft.agent,
                                      "outcome": outcome, "score": value})
    observability.score(draft.trace_id, "hr_review", value, f"{draft.kind} by {draft.agent}: {outcome}")


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
    edited = ((body.content is not None and body.content != draft.content)
              or (body.subject is not None and body.subject != draft.subject))
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
    _score_review(draft, REVIEW_EDITED if edited else REVIEW_APPROVED,
                  "approved after edits" if edited else "approved unchanged")
    return draft_out(draft)


@router.post("/drafts/{draft_id}/discard")
def discard(draft_id: int, db: Session = Depends(get_db), user: User = Depends(require_hr)):
    draft = _pending_or_error(db, draft_id)
    draft.status = "discarded"
    draft.reviewed_by = user.id
    draft.reviewed_at = utcnow()
    db.commit()
    _score_review(draft, REVIEW_DISCARDED, "discarded")
    return draft_out(draft)
