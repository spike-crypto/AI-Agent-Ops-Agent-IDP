"""AgentOps tracing. Optional: if AGENTOPS_API_KEY is missing, or the SDK changes,
the app keeps working and the local AgentRun audit log is still written."""

import logging
from contextlib import contextmanager

from app.config import settings

log = logging.getLogger("hr_agents.observability")
_agentops = None


def init_observability() -> None:
    global _agentops
    if not settings.agentops_api_key:
        log.info("AGENTOPS_API_KEY not set; AgentOps tracing is off.")
        return
    try:
        import agentops

        # AgentOps auto-instruments the OpenAI client, so every LLM call is captured.
        agentops.init(
            api_key=settings.agentops_api_key,
            default_tags=["hr-agents"],
            auto_start_session=False,
        )
        _agentops = agentops
        log.info("AgentOps tracing is on.")
    except Exception as exc:
        log.warning("AgentOps failed to start, continuing without it: %s", exc)


@contextmanager
def trace(agent: str, action: str, user_email: str):
    """Wraps one agent run in an AgentOps trace named after the agent and action."""
    handle = None
    if _agentops is not None:
        try:
            handle = _agentops.start_trace(
                trace_name=f"{agent}.{action}", tags=[agent, action, f"user:{user_email}"]
            )
        except Exception as exc:
            log.warning("AgentOps start_trace failed: %s", exc)
    try:
        yield
    except Exception:
        _end(handle, "Fail")
        raise
    _end(handle, "Success")


def _end(handle, state: str) -> None:
    if handle is None or _agentops is None:
        return
    try:
        _agentops.end_trace(handle, end_state=state)
    except Exception as exc:
        log.warning("AgentOps end_trace failed: %s", exc)
