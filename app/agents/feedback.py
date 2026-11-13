from app.agents.base import Agent, AgentContext, draft_to_dict
from app.prompts import Prompt

REMINDER_SYSTEM = Prompt(
    "feedback.reminder", 1,
    "You write very short, polite reminder messages asking an interviewer to submit their "
    "interview feedback. One short paragraph. Sign off as 'HR'."
)
SUMMARY_SYSTEM = Prompt(
    "feedback.summary", 1,
    "You summarise interview feedback for an HR person making a hiring decision. "
    "Be balanced: agreement, disagreement, strengths, concerns, and the average rating. "
    "Use only what the interviewers wrote. Do not make the hiring decision yourself."
)
DECISION_SYNTHESIS_SYSTEM = Prompt(
    "feedback.decision_memo", 1,
    "You are an executive HR hiring assistant helping a hiring manager evaluate a candidate after all interviews. "
    "Synthesize the interviewers' feedback into an actionable Hiring Decision Recommendation Memo.\n"
    "Structure your memo clearly with:\n"
    "1. Executive Summary & Consensus: Overall recommendation (Strong Hire / Lean Hire / Lean No Hire / Strong No Hire) based on ratings.\n"
    "2. Key Strengths Demonstrated across interview rounds.\n"
    "3. Reservations, Gaps & Red Flags noted by interviewers.\n"
    "4. Suggested Next Steps (e.g. advance to offer negotiation, request a targeted follow-up conversation, or send polite rejection).\n"
    "Keep it concise, objective, and professional. Never invent facts."
)


class FeedbackAgent(Agent):
    name = "feedback"
    description = "Finds interviews with missing feedback, drafts reminders, and summarises feedback."
    scopes = frozenset({"job:read", "candidate:read", "candidate:write", "interview:read", "feedback:read", "draft:write"})
    actions = ("chase", "summarize", "synthesize")

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

    def synthesize(self, ctx: AgentContext, candidate_id: int) -> dict:
        cand = ctx.candidate(candidate_id)
        job = ctx.job(cand.job_id)
        rows = ctx.feedback_for_candidate(candidate_id)
        if not rows:
            raise ValueError(f"No interview feedback submitted yet for {cand.name}")

        lines = "\n\n".join(f"Interviewer: {u.name} ({u.role})\nRating: {f.rating}/5\nFeedback Notes:\n{f.notes}" for f, u in rows)
        avg = sum(f.rating for f, _ in rows) / len(rows)

        prompt = (
            f"Candidate: {cand.name}\n"
            f"Job Role: {job['title']}\n"
            f"Job Requirements: {job['requirements']}\n"
            f"Average Rating: {round(avg, 1)}/5 ({len(rows)} interview(s))\n\n"
            f"INTERVIEW FEEDBACK DETAILS:\n{lines}"
        )

        content = ctx.llm.complete(DECISION_SYNTHESIS_SYSTEM, prompt)

        draft = ctx.create_draft(
            kind="decision_memo",
            subject=f"Hiring Decision Recommendation: {cand.name} – {job['title']}",
            content=content,
            candidate_id=cand.id,
            job_id=job["id"],
        )

        return {
            "summary": f"Synthesized hiring decision recommendation for {cand.name}",
            "average_rating": round(avg, 1),
            "feedback_count": len(rows),
            "draft": draft_to_dict(draft),
        }
