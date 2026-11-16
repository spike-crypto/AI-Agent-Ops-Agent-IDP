from app.agents.base import Agent

# What an integration key may ever carry. Drafts and feedback stay inside Hiring Desk.
INTEGRATION_SCOPES = frozenset({"job:read", "candidate:read", "interview:read"})


class IntegrationAgent(Agent):
    """The identity outside systems (SAP Integration Suite, MCP clients) act through.

    It has no actions of its own: each call from outside receives a single-use token for
    `agent:integration` carrying read scopes only, and runs one read tool with it.
    """

    name = "integration"
    description = "Reads jobs, candidates and interviews for an outside system using an integration key."
    scopes = INTEGRATION_SCOPES
    actions = ()
