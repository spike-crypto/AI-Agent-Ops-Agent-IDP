"""Every agent the app knows about. Add new agents here."""

from app.agents.feedback import FeedbackAgent
from app.agents.scheduling import SchedulingAgent
from app.agents.screening import ScreeningAgent
from app.agents.writing import WritingAgent

AGENTS = {
    agent.name: agent
    for agent in (
        WritingAgent(),
        ScreeningAgent(),
        SchedulingAgent(),
        FeedbackAgent(),
    )
}
