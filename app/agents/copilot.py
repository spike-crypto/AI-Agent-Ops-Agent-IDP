"""The HR Copilot: turns a plain-language request into steps and delegates each to its agent.
Its tools and scopes live in app/copilot_tools.py."""

from app.agents.base import Agent
from app.copilot_tools import COPILOT_SCOPES


class CopilotAgent(Agent):
    name = "copilot"
    description = "Answers HR's questions in plain language by delegating each step to the right agent."
    scopes = COPILOT_SCOPES
    actions: tuple[str, ...] = ()
