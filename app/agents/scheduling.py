from datetime import datetime

from app.agents.base import Agent, AgentContext, draft_to_dict

SYSTEM = (
    "You write short, friendly interview invitation emails. List the offered time slots "
    "exactly as given and ask the candidate to reply with the one that suits them. "
    "Do not invent locations or links. Sign off as 'The Hiring Team'."
)


class SchedulingAgent(Agent):
    name = "scheduling"
    description = "Sets up an interview with proposed slots and drafts the invite email."
    scopes = frozenset({"job:read", "candidate:read", "interview:write", "draft:write"})
    actions = ("propose",)

    def propose(self, ctx: AgentContext, candidate_id: int, interviewer_id: int, slots: list[str]) -> dict:
        if not slots:
            raise ValueError("Give at least one time slot")
        try:
            parsed = sorted(datetime.fromisoformat(s) for s in slots)
        except ValueError:
            raise ValueError("Slots must be ISO date-times, e.g. 2026-11-20T10:00")
        clean = [p.strftime("%Y-%m-%dT%H:%M") for p in parsed]

        cand = ctx.candidate(candidate_id)
        job = ctx.job(cand.job_id)
        interviewer = ctx.interviewer(interviewer_id)
        interview = ctx.create_interview(cand, interviewer, clean)

        readable = "\n".join(f"- {p.strftime('%A %d %B %Y, %H:%M')}" for p in parsed)
        prompt = (
            f"Invite {cand.name} to an interview for the role {job['title']} "
            f"with {interviewer.name}.\nOffered slots:\n{readable}"
        )
        text = ctx.llm.complete(SYSTEM, prompt)
        draft = ctx.create_draft("invite", f"Interview invitation: {job['title']}", text,
                                 recipient=cand.email, job_id=job["id"], candidate_id=cand.id,
                                 interview_id=interview.id)
        return {"summary": f"Proposed {len(clean)} slots for {cand.name} with {interviewer.name}",
                "interview_id": interview.id, "draft": draft_to_dict(draft)}
