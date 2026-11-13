"""Quality check for the screening agent.

A fixed set of sample resumes with the score range each should land in, plus a few
"this one must score above that one" checks. Run it against the real model whenever
the screening prompt or the model list changes:

    python -m evals.screening

Exit code: 0 all passed, 1 at least one wrong answer, 2 inconclusive (the model could
not be reached for some cases, e.g. rate limits, and nothing it did answer was wrong).
With Langfuse on, each case is a trace named
`eval.screening` carrying an `eval_pass` score, so runs can be compared per prompt version.
"""

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from app import observability
from app.agents.screening import SYSTEM, ScreeningAgent
from app.llm import LLM, LLMError

ROOT = Path(__file__).resolve().parent.parent
CASES_FILE = Path(__file__).resolve().parent / "screening_cases.json"


@dataclass
class CaseResult:
    name: str
    resume: str
    low: int
    high: int
    score: int | None = None
    error: str = ""

    @property
    def passed(self) -> bool:
        return self.score is not None and self.low <= self.score <= self.high

    @property
    def errored(self) -> bool:
        """The model gave no usable answer, so this says nothing about quality."""
        return self.score is None

    @property
    def status(self) -> str:
        return "ERROR" if self.errored else "PASS" if self.passed else "FAIL"


@dataclass
class EvalReport:
    prompt: str
    ordering_total: int = 0
    cases: list[CaseResult] = field(default_factory=list)
    ordering_failures: list[str] = field(default_factory=list)
    ordering_skipped: list[str] = field(default_factory=list)

    @property
    def wrong(self) -> int:
        return sum(c.status == "FAIL" for c in self.cases) + len(self.ordering_failures)

    @property
    def could_not_run(self) -> int:
        return sum(c.errored for c in self.cases) + len(self.ordering_skipped)

    @property
    def passed(self) -> bool:
        return self.wrong == 0 and self.could_not_run == 0

    @property
    def inconclusive(self) -> bool:
        return self.wrong == 0 and self.could_not_run > 0

    @property
    def exit_code(self) -> int:
        return 0 if self.passed else 2 if self.inconclusive else 1


def load_cases() -> dict:
    return json.loads(CASES_FILE.read_text(encoding="utf-8"))


def run(llm: LLM, spec: dict | None = None, pause_seconds: float = 0.0) -> EvalReport:
    spec = spec or load_cases()
    job = spec["job"]
    report = EvalReport(prompt=SYSTEM.label, ordering_total=len(spec.get("ordering", [])))
    scores: dict[str, int | None] = {}
    # One Langfuse session per eval run, so its cases can be read together.
    session = f"eval-screening-{time.strftime('%Y%m%d-%H%M%S')}"

    for i, case in enumerate(spec["cases"]):
        if i and pause_seconds:
            time.sleep(pause_seconds)  # free models rate-limit bursts
        result = CaseResult(case["name"], case["resume"], case["min"], case["max"])
        text = (ROOT / "sample_data" / case["resume"]).read_text(encoding="utf-8")
        with observability.trace("eval", "screening", "eval-runner",
                                 {"case": case["name"], "prompt": SYSTEM.label}, session) as run_handle:
            trace_id = observability.current_trace_id()
            try:
                data = ScreeningAgent._score(llm, job, text)
                result.score = max(0, min(100, int(data.get("score", 0))))
            except (LLMError, ValueError, TypeError) as exc:
                result.error = str(exc)[:200]
            run_handle.record(output=f"score {result.score}, expected {result.low}-{result.high}",
                              metadata={"case": case["name"], "prompt": SYSTEM.label})
        if not result.errored:  # an unreachable model is not a quality signal
            observability.score(trace_id, "eval_pass", 1.0 if result.passed else 0.0,
                                f"{case['name']}: got {result.score}, expected {result.low}-{result.high}")
        scores[case["resume"]] = result.score
        report.cases.append(result)

    for rule in spec.get("ordering", []):
        hi, lo = scores.get(rule["higher"]), scores.get(rule["lower"])
        if hi is None or lo is None:
            report.ordering_skipped.append(f"{rule['higher']} above {rule['lower']}")
        elif hi <= lo:
            report.ordering_failures.append(
                f"{rule['higher']} ({hi}) should score above {rule['lower']} ({lo})")
    return report


def render(report: EvalReport) -> str:
    lines = [f"Screening eval, prompt {report.prompt}", ""]
    for c in report.cases:
        got = "error" if c.score is None else str(c.score)
        line = f"  {c.status:5}  {c.name:46} got {got:>5}  expected {c.low}-{c.high}"
        lines.append(line + (f"  ({c.error[:90]})" if c.error else ""))
    lines += [f"  FAIL   order: {f}" for f in report.ordering_failures]
    lines += [f"  SKIP   order: {s} (a score is missing)" for s in report.ordering_skipped]
    total = len(report.cases) + report.ordering_total
    verdict = "PASSED" if report.passed else "INCONCLUSIVE" if report.inconclusive else "FAILED"
    lines += ["", f"{verdict}: {total - report.wrong - report.could_not_run} of {total} checks passed, "
                  f"{report.wrong} wrong, {report.could_not_run} could not run"]
    return "\n".join(lines)


def main() -> None:
    from app.llm import get_llm_optional
    from app.observability import init_observability, shutdown_observability

    llm = get_llm_optional()
    if llm is None:
        sys.exit("No model configured: set OPENROUTER_API_KEY in .env first.")
    init_observability()
    try:
        report = run(llm, pause_seconds=3.0)
    finally:
        shutdown_observability()
    print(render(report))
    sys.exit(report.exit_code)


if __name__ == "__main__":
    # Run through the imported module so there is one copy of it (see app/prompts.py).
    from evals.screening import main as _main

    _main()
