from app.agents.base import Agent, AgentContext, draft_to_dict

SYSTEM = (
    "You are an HR writing assistant for a small company. Write clear, warm, inclusive, "
    "professional text in plain prose (no markdown tables). Never invent facts such as "
    "benefits, dates or numbers that were not given to you. Keep it concise."
)


class WritingAgent(Agent):
    name = "writing"
    description = "Drafts job descriptions, rejection emails and offer letters for HR to review."
    scopes = frozenset({"job:read", "job:salary:read", "candidate:read", "draft:write"})
    actions = ("job_description", "rejection", "offer")

    def job_description(self, ctx: AgentContext, job_id: int) -> dict:
        job = ctx.job(job_id)
        prompt = (
            f"Write a job description for: {job['title']}\n"
            f"Department: {job['department'] or 'not specified'}\n"
            f"Must-have requirements:\n{job['requirements']}\n\n"
            "Include: a short summary, responsibilities, requirements, and nice-to-haves."
        )
        text = ctx.llm.complete(SYSTEM, prompt)
        draft = ctx.create_draft("job_description", f"Job description: {job['title']}", text, job_id=job_id)
        return {"summary": f"Drafted job description for '{job['title']}'", "draft": draft_to_dict(draft)}

    def rejection(self, ctx: AgentContext, candidate_id: int) -> dict:
        cand = ctx.candidate(candidate_id)
        job = ctx.job(cand.job_id)
        prompt = (
            f"Write a short, kind rejection email to {cand.name} who applied for {job['title']}.\n"
            "Thank them, do not give specific reasons, and wish them well. Sign off as 'The Hiring Team'."
        )
        text = ctx.llm.complete(SYSTEM, prompt)
        draft = ctx.create_draft("rejection", f"Your application for {job['title']}", text,
                                 recipient=cand.email, job_id=job["id"], candidate_id=cand.id)
        return {"summary": f"Drafted rejection for {cand.name}", "draft": draft_to_dict(draft)}

    def offer(self, ctx: AgentContext, candidate_id: int, start_date: str = "", notes: str = "") -> dict:
        cand = ctx.candidate(candidate_id)
        job = ctx.job(cand.job_id)
        if not job.get("salary_band"):
            raise ValueError("Set a salary band on the job before drafting an offer")
        prompt = (
            f"Write an offer letter to {cand.name} for the role {job['title']}.\n"
            f"Compensation: {job['salary_band']}\n"
            f"Start date: {start_date or 'to be agreed'}\n"
            f"Extra notes from HR: {notes or 'none'}\n"
            "Ask them to reply to accept. Sign off as 'The Hiring Team'."
        )
        text = ctx.llm.complete(SYSTEM, prompt)
        draft = ctx.create_draft("offer", f"Offer: {job['title']}", text,
                                 recipient=cand.email, job_id=job["id"], candidate_id=cand.id)
        return {"summary": f"Drafted offer for {cand.name}", "draft": draft_to_dict(draft)}
