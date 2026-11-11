from app.agents.base import Agent, AgentContext, draft_to_dict

SYSTEM = (
    "You are an expert technical recruiter and hiring manager assistant. "
    "Your task is to generate a comprehensive, tailored Interview Kit for an upcoming interview. "
    "Structure your output clearly with:\n"
    "1. 6 to 8 tailored interview questions categorized into:\n"
    "   - Technical and role-specific depth questions\n"
    "   - Resume-based experience follow-up questions\n"
    "   - Targeted questions addressing identified screening gaps\n"
    "2. An evaluation rubric explaining what good answers look like versus red flags.\n"
    "Base your evaluation strictly on job-relevant skills, candidate experience, and job requirements. "
    "Do not assume or invent facts."
)


class InterviewKitAgent(Agent):
    name = "interview_kit"
    description = (
        "Prepares tailored interview questions and an evaluation rubric based on the "
        "candidate's resume, job requirements, and screening gaps."
    )
    scopes = frozenset({"job:read", "candidate:read", "interview:read", "draft:write"})
    actions = ("prepare",)

    def prepare(self, ctx: AgentContext, interview_id: int) -> dict:
        interview = ctx.interview(interview_id)
        cand = ctx.candidate(interview.candidate_id)
        job = ctx.job(cand.job_id)

        # Extract screening gaps and strengths if available to guide question generation
        screening_info = cand.screening or {}
        gaps = screening_info.get("gaps", [])
        strengths = screening_info.get("strengths", [])

        gaps_text = "\n".join(f"- {g}" for g in gaps) if gaps else "None noted"
        strengths_text = "\n".join(f"- {s}" for s in strengths) if strengths else "None noted"

        prompt = (
            f"Generate an interview kit for candidate '{cand.name}' applying for '{job['title']}'.\n\n"
            f"JOB REQUIREMENTS:\n{job['requirements']}\n\n"
            f"IDENTIFIED SCREENING STRENGTHS:\n{strengths_text}\n\n"
            f"IDENTIFIED SCREENING GAPS:\n{gaps_text}\n\n"
            f"CANDIDATE RESUME:\n<<<\n{cand.resume_text[:12000]}\n>>>\n\n"
            "Requirements for the output:\n"
            "- Provide 6 to 8 numbered interview questions covering technical skills, resume claims, and screening gaps.\n"
            "- For each question, provide a short rubric on what a strong answer vs. poor answer demonstrates.\n"
            "- Include overall interviewer evaluation guidelines."
        )

        content = ctx.llm.complete(SYSTEM, prompt)

        draft = ctx.create_draft(
            kind="interview_kit",
            subject=f"Interview Kit: {cand.name} – {job['title']}",
            content=content,
            recipient=cand.email,
            job_id=job["id"],
            candidate_id=cand.id,
            interview_id=interview.id,
        )

        return {
            "summary": f"Drafted interview kit for {cand.name} ({job['title']})",
            "interview_id": interview.id,
            "draft": draft_to_dict(draft),
        }
