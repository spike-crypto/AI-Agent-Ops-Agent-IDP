"""Deterministic test double. This lives in tests/ on purpose: the shipped app has no
"fake AI" mode, and every AI failure is visible instead of being papered over."""

import json
import re

from app.llm import LLMError, vocabulary

_STOP = {
    "and", "the", "with", "for", "you", "our", "are", "years", "year", "experience",
    "of", "in", "to", "a", "an", "or", "on", "at", "is", "be", "as", "we", "will",
}


def _keywords(text: str) -> set[str]:
    return vocabulary(text) - _STOP


class StubLLM:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str, bool]] = []
        self.last_call = {"model": "stub"}

    def complete(self, system: str, prompt: str, json_mode: bool = False) -> str:
        self.calls.append((system, prompt, json_mode))
        if self.fail:
            raise LLMError("stub model failure")
        if json_mode and "CANDIDATES:" in prompt:
            return self._compare(prompt)
        if json_mode and "REQUIREMENTS:" in prompt and "RESUME:" in prompt:
            return self._screen(prompt)
        if json_mode and "health check" in system.lower():
            return json.dumps({"ok": True, "service": "hiring-desk"})
        if "interview kit" in prompt.lower() or "tailored interview questions" in prompt.lower():
            return (
                "## Interview Kit\n\n"
                "### Questions\n"
                "1. [Technical] How do you design scalable APIs with FastAPI and handle async database operations?\n"
                "   - Strong answer: Explains async I/O, connection pooling and non-blocking calls.\n"
                "2. [Technical] How do you structure Docker containers for production deployments?\n"
                "   - Strong answer: Mentions multi-stage builds and non-root users.\n"
                "3. [Resume Follow-up] What challenges arose in the migration you mentioned?\n"
                "   - Strong answer: Concrete latency, tracing and consistency details.\n"
                "4. [Resume Follow-up] How did you monitor services and find bottlenecks?\n"
                "   - Strong answer: Metrics, tracing and structured logs.\n"
                "5. [Screening Gap] Walk us through your hands-on CI/CD experience.\n"
                "   - Strong answer: PR checks, artifacts and zero-downtime deploys.\n"
                "6. [Screening Gap] How do you handle schema migrations and rollbacks?\n"
                "   - Strong answer: Versioned migrations and backward-compatible schemas.\n"
                "7. [Behavioral] Describe a production incident you triaged.\n"
                "   - Strong answer: Clear incident response and prevention.\n\n"
                "### Evaluation Rubric\n"
                "- High Hire (4-5): Concrete examples and trade-offs.\n"
                "- Mixed/Low Hire (1-2): Vague answers without examples."
            )
        first_line = prompt.strip().splitlines()[0] if prompt.strip() else ""
        return f"[Stub draft]\n\n{first_line}\n\nEdit before sending."

    @staticmethod
    def _screen(prompt: str) -> str:
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

    @staticmethod
    def _compare(prompt: str) -> str:
        block = prompt.split("REQUIREMENTS:", 1)[1].split("CANDIDATES:", 1)[0]
        wanted = _keywords(block)
        out = []
        for chunk in prompt.split("### CANDIDATE id=")[1:]:
            cid = int(re.match(r"\d+", chunk).group(0))
            have = _keywords(chunk.split("RESUME:", 1)[-1])
            out.append({
                "id": cid,
                "strengths": sorted(wanted & have)[:4],
                "risks": [f"no mention of {w}" for w in sorted(wanted - have)[:3]],
            })
        return json.dumps({
            "overview": f"Stub comparison of {len(out)} candidates.",
            "candidates": out,
            "open_questions": ["Ask for a concrete example of the strongest requirement."],
        })
