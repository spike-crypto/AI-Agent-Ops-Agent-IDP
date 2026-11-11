"""One small LLM interface so the provider can be swapped in one place.

- OpenRouterLLM: real calls through OpenRouter's OpenAI-compatible API, with model fallback.
- FakeLLM: no network, deterministic. Used by tests, CI and local runs without a key.
"""

import json
import re
from functools import lru_cache
from typing import Protocol

from app.config import settings


class LLMError(Exception):
    pass


class LLM(Protocol):
    def complete(self, system: str, prompt: str, json_mode: bool = False) -> str: ...


class OpenRouterLLM:
    def __init__(self, api_key: str, models: list[str]) -> None:
        from openai import OpenAI  # imported here so FakeLLM users don't need it configured

        # max_retries makes the client back off and retry on 429 rate limits and 5xx errors.
        self._client = OpenAI(
            base_url="https://openrouter.ai/api/v1", api_key=api_key, max_retries=3, timeout=60
        )
        self._models = models

    def complete(self, system: str, prompt: str, json_mode: bool = False) -> str:
        last_error: Exception | None = None
        for model in self._models:
            try:
                resp = self._client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.2,
                )
                text = (resp.choices[0].message.content or "").strip()
                if text:
                    return text
                last_error = LLMError(f"{model} returned an empty answer")
            except Exception as exc:  # try the next model
                last_error = exc
        raise LLMError(f"All models failed. Last error: {last_error}")


_WORD = re.compile(r"[a-zA-Z][a-zA-Z+#.\-]{1,}")
_STOP = {"and", "the", "with", "for", "you", "our", "are", "years", "year", "experience",
         "of", "in", "to", "a", "an", "or", "on", "at", "is", "be", "as", "we", "will"}


def _keywords(text: str) -> set[str]:
    return {w.lower().strip(".-") for w in _WORD.findall(text)} - _STOP


class FakeLLM:
    """Answers well enough to demo the whole flow offline.

    For screening it scores by keyword overlap between the REQUIREMENTS and RESUME
    sections of the prompt, so rankings are still meaningful without a real model.
    """

    def complete(self, system: str, prompt: str, json_mode: bool = False) -> str:
        if json_mode and "REQUIREMENTS:" in prompt and "RESUME:" in prompt:
            req = prompt.split("REQUIREMENTS:", 1)[1].split("RESUME:", 1)[0]
            resume = prompt.split("RESUME:", 1)[1]
            wanted, have = _keywords(req), _keywords(resume)
            matched = sorted(wanted & have)
            missing = sorted(wanted - have)
            score = round(100 * len(matched) / len(wanted)) if wanted else 0
            return json.dumps({
                "score": score,
                "summary": f"Matches {len(matched)} of {len(wanted)} requirement keywords.",
                "strengths": matched[:5],
                "gaps": missing[:5],
            })
        if "interview kit" in prompt.lower() or "tailored interview questions" in prompt.lower():
            return (
                "## Interview Kit\n\n"
                "### Questions\n"
                "1. [Technical] How do you design scalable APIs with FastAPI and handle async database operations?\n"
                "   - Strong answer: Explains async event loop, connection pooling, and non-blocking I/O.\n"
                "2. [Technical] How do you structure Docker containers for production deployments?\n"
                "   - Strong answer: Mentions multi-stage builds, non-root users, and minimal base images.\n"
                "3. [Resume Follow-up] In your resume you mentioned leading a microservices migration. What challenges arose?\n"
                "   - Strong answer: Describes concrete latency, distributed tracing, and data consistency issues.\n"
                "4. [Resume Follow-up] How did you monitor services and track performance bottlenecks?\n"
                "   - Strong answer: Discusses metrics, APM tracing, and structured logging.\n"
                "5. [Screening Gap] Can you walk us through your hands-on experience with automated CI/CD pipelines?\n"
                "   - Strong answer: Demonstrates clear understanding of PR checks, artifact packaging, and zero-downtime deploys.\n"
                "6. [Screening Gap] How do you handle schema migrations and rollback strategies in production?\n"
                "   - Strong answer: Covers Alembic/Flyway versioning, backward-compatible schemas, and blue/green deploys.\n"
                "7. [Behavioral & Problem Solving] Describe a situation where a service failed in production. How did you triage?\n"
                "   - Strong answer: Explains incident response, post-mortems, and preventive alerting.\n\n"
                "### Evaluation Rubric\n"
                "- High Hire (4-5): Demonstrates deep architectural reasoning, mentions security/performance trade-offs, communicates clearly.\n"
                "- Mixed/Low Hire (1-2): Vague answers, cannot explain architecture decisions, lacks concrete examples."
            )
        first_line = prompt.strip().splitlines()[0] if prompt.strip() else ""
        return f"[Draft generated offline by FakeLLM]\n\n{first_line}\n\nPlease edit before sending."


def parse_json(text: str) -> dict:
    """Pull the first JSON object out of a model answer (models often wrap it in prose)."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise LLMError("Model did not return JSON")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"Model returned invalid JSON: {exc}") from exc


@lru_cache
def get_llm() -> LLM:
    if settings.llm_provider == "openrouter":
        if not settings.openrouter_api_key:
            raise LLMError("LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is not set")
        return OpenRouterLLM(settings.openrouter_api_key, settings.openrouter_models)
    return FakeLLM()
