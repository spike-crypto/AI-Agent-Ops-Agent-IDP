"""The HR Copilot: turns a plain-language request into steps and delegates each to its agent.
Its tools and scopes live in app/copilot_tools.py."""

from app.agents.base import Agent
from app.copilot_tools import COPILOT_SCOPES, READ_SCOPES


class CopilotAgent(Agent):
    name = "copilot"
    description = "Answers HR's questions in plain language by delegating each step to the right agent."
    # It can only read for itself. Write scopes ride on its token purely to be handed on,
    # each time narrowed to one target agent for one step.
    scopes = READ_SCOPES
    delegable_scopes = COPILOT_SCOPES - READ_SCOPES
    actions: tuple[str, ...] = ()
