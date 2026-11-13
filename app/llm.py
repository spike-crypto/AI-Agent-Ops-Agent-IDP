"""One LLM interface so the provider can be swapped in one place.

There is deliberately no offline "fake" mode in the shipped app: an HR tool that
looks like it is thinking while returning made-up text is worse than one that says
the AI is unavailable. Tests inject their own stub through FastAPI dependency
overrides (tests/stub_llm.py); nothing in app/ pretends to be a model.
"""

import json
import re
import threading
import time
from functools import lru_cache
from typing import Protocol

from app import observability
from app.config import settings
from app.logging_config import get_logger
from app.prompts import Prompt

log = get_logger("hr_agents.llm")


class LLMError(Exception):
    pass


class LLM(Protocol):
    def complete(self, system: str, prompt: str, json_mode: bool = False) -> str: ...


class OpenRouterLLM:
    def __init__(self, api_key: str, models: list[str]) -> None:
        from openai import OpenAI

        # max_retries makes the client back off and retry on 429 rate limits and 5xx errors.
        self._client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
            max_retries=settings.llm_max_retries,
            timeout=settings.llm_timeout_seconds,
            default_headers={
                "HTTP-Referer": settings.public_base_url or "https://github.com/spike-crypto",
                "X-Title": "Hiring Desk",
            },
        )
        self._models = models
        self._lock = threading.Lock()
        self.last_call: dict = {}

    @property
    def models(self) -> list[str]:
        return list(self._models)

    def complete(self, system: str, prompt: str, json_mode: bool = False) -> str:
        errors: list[str] = []
        # Which versioned prompt this is, so traces and logs can be compared across versions.
        prompt_meta = ({"prompt_name": system.name, "prompt_version": system.version}
                       if isinstance(system, Prompt) else {})
        for model in self._models:
            started = time.perf_counter()
            try:
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ]
                params = {"temperature": 0.2, "max_tokens": settings.llm_max_tokens}
                # Each attempt is its own Langfuse generation, so a failed model is visible too.
                with observability.generation(model, messages, params, prompt_meta) as gen:
                    resp = self._client.chat.completions.create(
                        model=model,
                        messages=messages,
                        **params,
                    )
                    text = (resp.choices[0].message.content or "").strip()
                    latency = int((time.perf_counter() - started) * 1000)
                    if not text:
                        raise LLMError("returned an empty answer")
                    if json_mode:
                        try:
                            parse_json(text)
                        except LLMError as exc:
                            # A free model occasionally answers in prose. Treat it like
                            # any other failed attempt and let the next model try.
                            raise LLMError(str(exc)) from None
                    usage = getattr(resp, "usage", None)
                    tokens = {
                        "prompt_tokens": getattr(usage, "prompt_tokens", None),
                        "completion_tokens": getattr(usage, "completion_tokens", None),
                    }
                    gen.record(output=text, usage={
                        k: v for k, v in (("input", tokens["prompt_tokens"]),
                                          ("output", tokens["completion_tokens"])) if v is not None
                    })
                with self._lock:
                    self.last_call = {"model": model, "latency_ms": latency, **tokens}
                log.info("llm.call_ok", extra={"model": model, "latency_ms": latency, **tokens, **prompt_meta})
                return text
            except Exception as exc:
                latency = int((time.perf_counter() - started) * 1000)
                errors.append(f"{model}: {type(exc).__name__}: {exc}")
                log.warning("llm.call_failed", extra={"model": model, "latency_ms": latency,
                                                      "error": str(exc)[:300]})
        raise LLMError("All models failed. " + " | ".join(errors))


_WORD = re.compile(r"[a-zA-Z][a-zA-Z+#.\-]{1,}")


def vocabulary(text: str) -> set[str]:
    """Lower-case word tokens, used by tests and by the public resume-match helper."""
    return {w.lower().strip(".-") for w in _WORD.findall(text)}


def parse_json(text: str) -> dict:
    """Pull the first JSON object out of a model answer.

    Models often wrap JSON in prose or ``` fences, and some emit a second object;
    raw_decode finds the first complete object without swallowing the rest.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            data, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise LLMError("Model did not return a JSON object")


def llm_status() -> dict:
    """Safe to expose on /health: no key, no prompts."""
    return {
        "provider": "openrouter",
        "configured": settings.llm_configured,
        "models": settings.openrouter_models,
        "timeout_seconds": settings.llm_timeout_seconds,
    }


@lru_cache
def get_llm() -> LLM:
    if not settings.llm_configured:
        raise LLMError(
            "OPENROUTER_API_KEY is not set. Add it to .env (or Render > Environment) "
            "and restart. AI actions are disabled until then."
        )
    return OpenRouterLLM(settings.openrouter_api_key, settings.openrouter_models)


def get_llm_optional() -> LLM | None:
    """FastAPI dependency: lets /health report "not configured" instead of a 500.

    Tests override this with a deterministic stub; the shipped app never fakes a model.
    """
    try:
        return get_llm()
    except LLMError:
        return None
