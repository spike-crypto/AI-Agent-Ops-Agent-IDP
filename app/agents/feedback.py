from app.agents.base import Agent, AgentContext, draft_to_dict

REMINDER_SYSTEM = (
    "You write very short, polite reminder messages asking an interviewer to submit their "
    "interview feedback. One short paragraph. Sign off as 'HR'."
)
SUMMARY_SYSTEM = (
    "You summarise interview feedback for an HR person making a hiring decision. "
    "Be balanced: agreement, disagreement, strengths, concerns, and the average rating. "
    "Use only what the interviewers wrote. Do not make the hiring decision yourself."
)


class FeedbackAgent(Agent):
    name = "feedback"
    description = "Finds interviews with missing feedback, drafts reminders, and summarises feedback."
    scopes = frozenset({"candidate:read", "candidate:write", "interview:read", "feedback:read", "draft:write"})
    actions = ("chase", "summarize")

    def chase(self, ctx: AgentContext) -> dict:
        overdue = ctx.overdue_interviews()
        drafts = []
        for interview, interviewer, cand in overdue:
            when = interview.scheduled_at.strftime("%d %B %Y, %H:%M")
            prompt = (
                f"Remind {interviewer.name} to submit feedback for their interview with "
                f"{cand.name} on {when}. They can submit it in the hiring app."
            )
            text = ctx.llm.complete(REMINDER_SYSTEM, prompt)
            d = ctx.create_draft("reminder", f"Feedback needed: {cand.name}", text,
                                 recipient=interviewer.email, candidate_id=cand.id,
                                 interview_id=interview.id)
            drafts.append(draft_to_dict(d))
        return {"summary": f"{len(overdue)} interview(s) missing feedback", "drafts": drafts}

    def summarize(self, ctx: AgentContext, candidate_id: int) -> dict:
        cand = ctx.candidate(candidate_id)
        rows = ctx.feedback_for_candidate(candidate_id)
        if not rows:
            raise ValueError(f"No feedback submitted yet for {cand.name}")
        lines = "\n\n".join(f"Interviewer: {u.name}\nRating: {f.rating}/5\nNotes: {f.notes}" for f, u in rows)
        text = ctx.llm.complete(SUMMARY_SYSTEM, f"Candidate: {cand.name}\n\nFEEDBACK:\n{lines}")
        ctx.save_feedback_summary(cand, text)
        avg = sum(f.rating for f, _ in rows) / len(rows)
        return {"summary": f"Summarised {len(rows)} feedback(s) for {cand.name}",
                "average_rating": round(avg, 1), "text": text}
