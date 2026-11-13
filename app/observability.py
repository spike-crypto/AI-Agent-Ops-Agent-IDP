"""Langfuse tracing: one trace per agent run, with each model call nested inside it.

Optional and fail-safe. Without LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY nothing is
sent anywhere, and if Langfuse itself misbehaves the agent still runs: the local AgentRun
audit log never depends on it.

Privacy: prompts contain resume text. Set LANGFUSE_CAPTURE_CONTENT=false to send only
names, timings, models and token counts, and no prompt or answer text.
"""

import logging
from contextlib import ExitStack, contextmanager
from typing import Any, Iterator

from app.config import settings

log = logging.getLogger("hr_agents.observability")
_client = None


def enabled() -> bool:
    return _client is not None


def init_observability(span_exporter: Any = None) -> None:
    """Start the Langfuse client if keys are configured. `span_exporter` is for tests."""
    global _client
    if _client is not None:
        return
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        log.info("Langfuse keys not set; tracing is off.")
        return
    try:
        from langfuse import Langfuse

        kwargs: dict[str, Any] = {}
        if span_exporter is not None:
            kwargs["span_exporter"] = span_exporter
        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            base_url=settings.langfuse_host,
            environment=settings.app_env,
            **kwargs,
        )
        log.info("Langfuse tracing is on (%s, content capture %s).", settings.langfuse_host,
                 "on" if settings.langfuse_capture_content else "off")
    except Exception as exc:
        log.warning("Langfuse failed to start, continuing without it: %s", exc)


def flush() -> None:
    if _client is None:
        return
    try:
        _client.flush()
    except Exception as exc:
        log.warning("Langfuse flush failed: %s", exc)


def shutdown_observability() -> None:
    """Send what is still buffered, then stop. Called when the app shuts down."""
    global _client
    if _client is None:
        return
    try:
        _client.flush()
        _client.shutdown()
    except Exception as exc:
        log.warning("Langfuse shutdown failed: %s", exc)
    _client = None


class Handle:
    """What `trace()` and `generation()` yield. Every method is a no-op when tracing is off."""

    def __init__(self, observation: Any = None) -> None:
        self._obs = observation

    def record(self, *, output: Any = None, metadata: dict | None = None,
               usage: dict | None = None) -> None:
        if self._obs is None:
            return
        fields: dict[str, Any] = {}
        if output is not None and settings.langfuse_capture_content:
            fields["output"] = output
        if metadata:
            fields["metadata"] = metadata
        if usage:
            fields["usage_details"] = usage
        if not fields:
            return
        try:
            self._obs.update(**fields)
        except Exception as exc:
            log.warning("Langfuse update failed: %s", exc)

    def _fail(self, exc: BaseException) -> None:
        if self._obs is None:
            return
        try:
            self._obs.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}"[:500])
        except Exception as update_exc:
            log.warning("Langfuse update failed: %s", update_exc)


def _close(stack: ExitStack) -> None:
    try:
        stack.close()
    except Exception as exc:
        log.warning("Langfuse failed to end an observation: %s", exc)


@contextmanager
def _observe(attributes: dict | None, **observation: Any) -> Iterator[Handle]:
    """Open a Langfuse observation around the body. Errors in Langfuse are logged and
    swallowed; errors in the body are marked on the observation and re-raised."""
    if _client is None:
        yield Handle()
        return

    stack = ExitStack()
    handle = Handle()
    try:
        if not settings.langfuse_capture_content:
            observation.pop("input", None)
        handle = Handle(stack.enter_context(_client.start_as_current_observation(**observation)))
        if attributes:
            from langfuse import propagate_attributes

            stack.enter_context(propagate_attributes(**attributes))
    except Exception as exc:
        log.warning("Langfuse could not start '%s': %s", observation.get("name"), exc)
        _close(stack)
        stack, handle = ExitStack(), Handle()

    try:
        yield handle
    except BaseException as exc:
        handle._fail(exc)
        _close(stack)
        raise
    _close(stack)


def trace(agent: str, action: str, user_email: str, params: dict | None = None):
    """One agent run. Model calls made inside it show up as its children."""
    name = f"{agent}.{action}"
    return _observe(
        {"trace_name": name, "user_id": user_email, "tags": ["hiring-desk", agent, action]},
        name=name, as_type="agent", input=params or None,
    )


def generation(model: str, messages: list[dict], model_parameters: dict | None = None):
    """One call to a model. Use `handle.record(output=..., usage=...)` when it answers."""
    return _observe(
        None, name="llm.chat", as_type="generation", model=model, input=messages,
        model_parameters=model_parameters,
    )
