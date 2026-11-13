"""How agents are allowed to touch data.

An agent never gets the database. It gets an AgentContext, and every read or write
goes through a method that checks a scope first. What an agent may do is:

    effective scopes = scopes the agent declares  ∩  scopes the signed-in user's role has

So the screening agent can never see salary bands (it doesn't declare job:salary:read),
even when the HR admin, who can see them, is the one running it.
"""

from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import observability
from app.llm import LLM
from app.models import CANDIDATE_STATUSES, Candidate, Draft, Feedback, Interview, Job, User, utcnow

SCOPES = {
    "job:read": "Read job title, department, requirements and description",
    "job:salary:read": "Read the confidential salary band of a job",
    "candidate:read": "Read candidate profiles and resumes",
    "candidate:write": "Save screening scores and feedback summaries",
    "interview:read": "Read interviews",
    "interview:write": "Create interviews and look up interviewers",
    "feedback:read": "Read interviewer feedback",
    "draft:write": "Create drafts for HR to review (never sends anything)",
    "draft:read": "Read drafts waiting in the review queue",
    "candidate:stage:write": "Move a candidate to another hiring stage (only after HR approves)",
}

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "hr_admin": frozenset(SCOPES),
    "interviewer": frozenset({"job:read", "candidate:read", "interview:read"}),
}


class AgentPermissionError(Exception):
    pass


class AgentContext:
    def __init__(
        self,
        db: Session,
        user: User,
        llm: LLM,
        agent_name: str,
        granted: frozenset[str],
        progress: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ):
        self._db = db
        self.user = user
        self.llm = llm
        self.agent_name = agent_name
        self.granted = granted
        self.used: set[str] = set()
        self._progress = progress
        self._should_stop = should_stop

    def require(self, scope: str) -> None:
        if scope not in self.granted:
            raise AgentPermissionError(
                f"Agent '{self.agent_name}' is not allowed '{scope}' for role '{self.user.role}'"
            )
        self.used.add(scope)

    def report_progress(self, current: int, total: int) -> None:
        if self._progress is not None:
            self._progress(current, total)

    def checkpoint(self) -> None:
        """Make finished work durable. Long runs call this between items so a crash,
        cancellation or error never throws away scores that were already computed."""
        self._db.commit()

    def stopped(self) -> bool:
        return bool(self._should_stop and self._should_stop())

    # ---- reads -------------------------------------------------------------
    def job(self, job_id: int, include_salary: bool = False) -> dict:
        self.require("job:read")
        job = self._db.get(Job, job_id)
        if job is None:
            raise LookupError(f"Job {job_id} not found")
        data = {
            "id": job.id,
            "title": job.title,
            "department": job.department,
            "requirements": job.requirements,
            "description": job.description,
        }
        # Salary is only read when the action actually needs it (offer letters), so the
        # audit trail says "read salary" only when it was true.
        if include_salary and "job:salary:read" in self.granted:
            self.require("job:salary:read")
            data["salary_band"] = job.salary_band
        return data

    def candidate(self, candidate_id: int) -> Candidate:
        self.require("candidate:read")
        cand = self._db.get(Candidate, candidate_id)
        if cand is None:
            raise LookupError(f"Candidate {candidate_id} not found")
        return cand

    def candidates_for_job(self, job_id: int, statuses: tuple[str, ...] | None = None) -> list[Candidate]:
        self.require("candidate:read")
        q = select(Candidate).where(Candidate.job_id == job_id)
        if statuses:
            q = q.where(Candidate.status.in_(statuses))
        return list(self._db.scalars(q.order_by(Candidate.id)))

    def interviewer(self, user_id: int) -> User:
        self.require("interview:write")
        user = self._db.get(User, user_id)
        if user is None or not user.is_active:
            raise LookupError(f"Interviewer {user_id} not found")
        return user

    def interview(self, interview_id: int) -> Interview:
        self.require("interview:read")
        interview = self._db.get(Interview, interview_id)
        if interview is None:
            raise LookupError(f"Interview {interview_id} not found")
        return interview

    def interviewer_of(self, interview: Interview) -> User:
        """The person running an interview (read-only; for addressing interviewer-only drafts)."""
        self.require("interview:read")
        user = self._db.get(User, interview.interviewer_id)
        if user is None:
            raise LookupError(f"Interviewer for interview {interview.id} not found")
        return user

    def overdue_interviews(self) -> list[tuple[Interview, User, Candidate]]:
        """Scheduled interviews in the past that still have no feedback."""
        self.require("interview:read")
        self.require("feedback:read")
        q = (
            select(Interview, User, Candidate)
            .join(User, User.id == Interview.interviewer_id)
            .join(Candidate, Candidate.id == Interview.candidate_id)
            .outerjoin(Feedback, Feedback.interview_id == Interview.id)
            .where(Interview.status == "scheduled")
            .where(Interview.scheduled_at <= utcnow())
            .where(Feedback.id.is_(None))
        )
        return [tuple(row) for row in self._db.execute(q)]

    def feedback_for_candidate(self, candidate_id: int) -> list[tuple[Feedback, User]]:
        self.require("feedback:read")
        q = (
            select(Feedback, User)
            .join(Interview, Interview.id == Feedback.interview_id)
            .join(User, User.id == Feedback.interviewer_id)
            .where(Interview.candidate_id == candidate_id)
        )
        return [tuple(row) for row in self._db.execute(q)]

    # ---- writes ------------------------------------------------------------
    def save_screening(self, cand: Candidate, score: int, screening: dict) -> None:
        self.require("candidate:write")
        cand.score = score
        cand.screening = screening
        # Re-screening must not undo a human decision (shortlisted/rejected/offered).
        if cand.status == "new":
            cand.status = "screened"

    def set_stage(self, cand: Candidate, stage: str) -> str:
        """Move a candidate to another hiring stage. Only the Recruiter agent holds this scope,
        and the Copilot only reaches it after HR approves. Returns the previous stage."""
        self.require("candidate:stage:write")
        if stage not in CANDIDATE_STATUSES:
            raise ValueError(f"Unknown stage '{stage}'. Use one of: {', '.join(CANDIDATE_STATUSES)}")
        previous, cand.status = cand.status, stage
        return previous

    def save_feedback_summary(self, cand: Candidate, summary: str) -> None:
        self.require("candidate:write")
        cand.feedback_summary = summary

    def create_interview(self, cand: Candidate, interviewer: User, slots: list[str]) -> Interview:
        self.require("interview:write")
        interview = Interview(candidate_id=cand.id, interviewer_id=interviewer.id, proposed_slots=slots)
        if cand.status not in ("offered", "rejected"):
            cand.status = "interviewing"
        self._db.add(interview)
        self._db.flush()
        return interview

    def create_draft(self, kind: str, subject: str, content: str, recipient: str = "",
                     job_id: int | None = None, candidate_id: int | None = None,
                     interview_id: int | None = None) -> Draft:
        self.require("draft:write")
        draft = Draft(
            kind=kind, agent=self.agent_name, subject=subject, content=content, recipient=recipient,
            job_id=job_id, candidate_id=candidate_id, interview_id=interview_id,
            created_by=self.user.id, trace_id=observability.current_trace_id(),
        )
        self._db.add(draft)
        self._db.flush()
        return draft


class Agent:
    name: str = ""
    description: str = ""
    scopes: frozenset[str] = frozenset()
    # Scopes the agent may carry on its token only to hand on to other agents (token exchange),
    # never to use itself. Only an orchestrator such as the Copilot has any.
    delegable_scopes: frozenset[str] = frozenset()
    allowed_roles: frozenset[str] = frozenset({"hr_admin"})
    actions: tuple[str, ...] = ()

    def handle(self, ctx: AgentContext, action: str, params: dict) -> dict:
        if action not in self.actions:
            raise ValueError(f"Agent '{self.name}' has no action '{action}'")
        return getattr(self, action)(ctx, **params)


def draft_to_dict(d: Draft) -> dict:
    return {"id": d.id, "kind": d.kind, "subject": d.subject, "recipient": d.recipient,
            "content": d.content, "status": d.status}
