"""HR Copilot chat: conversations, background turns and approval of prepared changes.

Every conversation is private to the HR user who started it (others get 404). A message
starts a background `copilot.turn` job; the chat polls GET /agent-jobs/{id} for its steps.
An action card only changes the hiring record after POST /copilot/proposals/{id}/approve.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import observability
from app.db import get_db
from app.deps import require_hr
from app.jobqueue import enqueue
from app.logging_config import get_logger
from app.models import ActionProposal, Conversation, Message, User, utcnow

router = APIRouter(tags=["copilot"])
log = get_logger("hr_agents.copilot")


class NewConversation(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class NewMessage(BaseModel):
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("text")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Type a question first")
        return v.strip()


def _proposal_out(p: ActionProposal | None) -> dict | None:
    if p is None:
        return None
    return {"id": p.id, "status": p.status, "items": [{"label": i["label"], "tool": i["tool"]} for i in p.items],
            "result": (p.result or {}).get("steps")}


def _message_out(db: Session, m: Message) -> dict:
    return {"id": m.id, "role": m.role, "content": m.content, "steps": m.steps or [],
            "job_id": m.job_id, "created_at": m.created_at.isoformat(timespec="seconds"),
            "proposal": _proposal_out(db.get(ActionProposal, m.proposal_id) if m.proposal_id else None)}


def _own(db: Session, conversation_id: int, user: User) -> Conversation:
    conv = db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(404, "Conversation not found")
    return conv


@router.post("/copilot/conversations", status_code=201)
def create_conversation(body: NewConversation, db: Session = Depends(get_db), user: User = Depends(require_hr)):
    conv = Conversation(user_id=user.id, title=(body.title or "New chat").strip() or "New chat")
    db.add(conv)
    db.commit()
    return {"id": conv.id, "title": conv.title}


@router.get("/copilot/conversations")
def list_conversations(db: Session = Depends(get_db), user: User = Depends(require_hr)):
    rows = db.scalars(select(Conversation).where(Conversation.user_id == user.id)
                      .order_by(Conversation.updated_at.desc(), Conversation.id.desc()).limit(50))
    return [{"id": c.id, "title": c.title, "updated_at": c.updated_at.isoformat(timespec="seconds")} for c in rows]


@router.get("/copilot/conversations/{conversation_id}")
def get_conversation(conversation_id: int, db: Session = Depends(get_db), user: User = Depends(require_hr)):
    conv = _own(db, conversation_id, user)
    messages = db.scalars(select(Message).where(Message.conversation_id == conv.id).order_by(Message.id))
    return {"id": conv.id, "title": conv.title, "messages": [_message_out(db, m) for m in messages]}


@router.post("/copilot/conversations/{conversation_id}/messages", status_code=202)
def send_message(conversation_id: int, body: NewMessage, db: Session = Depends(get_db),
                 user: User = Depends(require_hr)):
    conv = _own(db, conversation_id, user)
    msg = Message(conversation_id=conv.id, role="user", content=body.text)
    db.add(msg)
    if conv.title == "New chat":
        conv.title = body.text[:60] + ("..." if len(body.text) > 60 else "")
    conv.updated_at = utcnow()
    db.commit()
    job = enqueue(db, user, "copilot", "turn", {"conversation_id": conv.id, "message_id": msg.id})
    msg.job_id = job.id
    db.commit()
    return {"message": _message_out(db, msg), "job_id": job.id}


def _decide(db: Session, proposal_id: int, user: User, new_status: str) -> ActionProposal:
    # Atomic: only one request can move a pending card, so approving twice applies it once.
    moved = db.execute(update(ActionProposal)
                       .where(ActionProposal.id == proposal_id, ActionProposal.user_id == user.id,
                              ActionProposal.status == "pending")
                       .values(status=new_status, decided_at=utcnow()))
    db.commit()
    proposal = db.get(ActionProposal, proposal_id)
    if proposal is None or proposal.user_id != user.id:
        raise HTTPException(404, "Proposal not found")
    if moved.rowcount != 1:
        raise HTTPException(409, f"These changes are already {proposal.status}")
    trace_id = (proposal.result or {}).get("trace_id")
    observability.score(trace_id, "copilot_action", 1.0 if new_status == "approved" else 0.0,
                        f"{len(proposal.items)} change(s) {new_status}")
    log.info("copilot.proposal_decided", extra={"proposal": proposal.id, "status": new_status,
                                                "items": len(proposal.items), "user": user.email})
    return proposal


@router.post("/copilot/proposals/{proposal_id}/approve", status_code=202)
def approve(proposal_id: int, db: Session = Depends(get_db), user: User = Depends(require_hr)):
    proposal = _decide(db, proposal_id, user, "approved")
    job = enqueue(db, user, "copilot", "approve", {"proposal_id": proposal.id})
    return {"proposal": _proposal_out(proposal), "job_id": job.id}


@router.post("/copilot/proposals/{proposal_id}/dismiss")
def dismiss(proposal_id: int, db: Session = Depends(get_db), user: User = Depends(require_hr)):
    proposal = _decide(db, proposal_id, user, "dismissed")
    db.add(Message(conversation_id=proposal.conversation_id, role="assistant",
                   content="Dismissed. Nothing was changed."))
    db.commit()
    return {"proposal": _proposal_out(proposal)}
