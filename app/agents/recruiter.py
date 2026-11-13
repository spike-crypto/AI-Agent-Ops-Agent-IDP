from app.agents.base import Agent, AgentContext


class RecruiterAgent(Agent):
    name = "recruiter"
    description = "Moves a candidate to another hiring stage. Used only for changes HR has approved."
    # The only agent that can change a stage. No model call, so no prompt.
    scopes = frozenset({"candidate:read", "candidate:stage:write"})
    actions = ("set_stage",)

    def set_stage(self, ctx: AgentContext, candidate_id: int, stage: str) -> dict:
        cand = ctx.candidate(int(candidate_id))
        previous = ctx.set_stage(cand, stage)
        return {"summary": f"Moved {cand.name} from {previous} to {stage}",
                "candidate_id": cand.id, "from": previous, "to": stage}
