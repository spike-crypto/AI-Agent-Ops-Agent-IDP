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
        if json_mode and "CANDIDATES:" in prompt:
            return self._compare(prompt)
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
        first_line = prompt.strip().splitlines()[0] if prompt.strip() else ""
        return f"[Draft generated offline by FakeLLM]\n\n{first_line}\n\nPlease edit before sending."

    def _compare(self, prompt: str) -> str:
        req = prompt.split("REQUIREMENTS:", 1)[1].split("CANDIDATES:", 1)[0]
        wanted = _keywords(req)
        out = []
        for block in prompt.split("### CANDIDATE id=")[1:]:
            cid = int(re.match(r"\d+", block).group(0))
            have = _keywords(block.split("RESUME:", 1)[-1])
            out.append({"id": cid, "strengths": sorted(wanted & have)[:4], "risks": [f"no mention of {w}" for w in sorted(wanted - have)[:3]]})
        return json.dumps({
            "overview": f"Offline comparison of {len(out)} candidates by requirement keywords.",
            "candidates": out,
            "open_questions": ["Ask each candidate for a concrete example of their strongest requirement."],
        })


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
