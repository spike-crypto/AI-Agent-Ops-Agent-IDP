"""How agents are allowed to touch data.

An agent never gets the database. It gets an AgentContext, and every read or write
goes through a method that checks a scope first. What an agent may do is:

    effective scopes = scopes the agent declares  ∩  scopes the signed-in user's role has

So the screening agent can never see salary bands (it doesn't declare job:salary:read),
even when the HR admin, who can see them, is the one running it.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm import LLM
from app.models import Candidate, Draft, Feedback, Interview, Job, User, utcnow

SCOPES = {
    "job:read": "Read job title, department, requirements and description",
    "job:salary:read": "Read the confidential salary band of a job",
    "candidate:read": "Read candidate profiles and resumes",
    "candidate:write": "Save screening scores and feedback summaries",
    "interview:read": "Read interviews",
    "interview:write": "Create interviews and look up interviewers",
    "feedback:read": "Read interviewer feedback",
    "draft:write": "Create drafts for HR to review (never sends anything)",
}

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "hr_admin": frozenset(SCOPES),
    "interviewer": frozenset({"job:read", "candidate:read", "interview:read"}),
}


class AgentPermissionError(Exception):
    pass


class AgentContext:
    def __init__(self, db: Session, user: User, llm: LLM, agent_name: str, granted: frozenset[str]):
        self._db = db
        self.user = user
        self.llm = llm
        self.agent_name = agent_name
        self.granted = granted
        self.used: set[str] = set()

    def require(self, scope: str) -> None:
        if scope not in self.granted:
            raise AgentPermissionError(
                f"Agent '{self.agent_name}' is not allowed '{scope}' for role '{self.user.role}'"
            )
        self.used.add(scope)

    # ---- reads -------------------------------------------------------------
    def job(self, job_id: int) -> dict:
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
        if "job:salary:read" in self.granted:  # only included when the agent is allowed
            self.used.add("job:salary:read")
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
        cand.status = "screened"

    def save_feedback_summary(self, cand: Candidate, summary: str) -> None:
        self.require("candidate:write")
        cand.feedback_summary = summary

    def create_interview(self, cand: Candidate, interviewer: User, slots: list[str]) -> Interview:
        self.require("interview:write")
        interview = Interview(candidate_id=cand.id, interviewer_id=interviewer.id, proposed_slots=slots)
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
            created_by=self.user.id,
        )
        self._db.add(draft)
        self._db.flush()
        return draft


class Agent:
    name: str = ""
    description: str = ""
    scopes: frozenset[str] = frozenset()
    allowed_roles: frozenset[str] = frozenset({"hr_admin"})
    actions: tuple[str, ...] = ()

    def handle(self, ctx: AgentContext, action: str, params: dict) -> dict:
        if action not in self.actions:
            raise ValueError(f"Agent '{self.name}' has no action '{action}'")
        return getattr(self, action)(ctx, **params)


def draft_to_dict(d: Draft) -> dict:
    return {"id": d.id, "kind": d.kind, "subject": d.subject, "recipient": d.recipient,
            "content": d.content, "status": d.status}
