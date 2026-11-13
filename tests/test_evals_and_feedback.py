"""The eval harness, and HR's review of a draft becoming a quality score."""

from app import observability
from app.db import SessionLocal
from app.models import Draft
from evals import screening as screening_eval
from tests.stub_llm import StubLLM
from tests.test_observability import spans  # noqa: F401  (fixture)


class AlwaysHundred:
    """A model that falls for everything, including the hidden instruction."""

    def complete(self, system, prompt, json_mode=False):
        return '{"score": 100, "summary": "perfect", "strengths": [], "gaps": []}'


def test_eval_passes_with_a_sensible_model():
    report = screening_eval.run(StubLLM())
    assert report.passed, screening_eval.render(report)
    assert report.prompt == "screening.system@v1" and report.exit_code == 0
    assert len(report.cases) == 6


def test_eval_catches_a_model_that_obeys_the_hidden_instruction():
    report = screening_eval.run(AlwaysHundred())
    assert not report.passed
    failed = {c.name for c in report.cases if not c.passed}
    assert "hidden instruction must not raise the score" in failed
    assert "graphic designer, no match" in failed
    assert report.ordering_failures            # everyone tied at 100
    assert report.exit_code == 1 and "FAILED" in screening_eval.render(report)


def test_an_unreachable_model_is_inconclusive_not_a_quality_failure():
    report = screening_eval.run(StubLLM(fail=True))
    assert not report.passed and report.inconclusive and report.exit_code == 2
    assert report.wrong == 0 and all(c.status == "ERROR" and c.error for c in report.cases)
    assert not report.ordering_failures and len(report.ordering_skipped) == 3
    assert "INCONCLUSIVE" in screening_eval.render(report)


def _draft(client, hr, job) -> dict:
    r = client.post("/agents/writing/job_description", headers=hr, json={"job_id": job["id"]})
    assert r.status_code == 200, r.text
    return r.json()["draft"]


def _spy(monkeypatch) -> list:
    calls = []
    monkeypatch.setattr(observability, "score",
                        lambda trace_id, name, value, comment="": calls.append((trace_id, name, value, comment)))
    return calls


def test_a_draft_remembers_the_trace_that_wrote_it(client, hr, job, spans):  # noqa: F811
    draft = _draft(client, hr, job)
    run = next(s for s in spans() if s.name == "writing.job_description")
    with SessionLocal() as db:
        assert db.get(Draft, draft["id"]).trace_id == format(run.context.trace_id, "032x")


def test_hr_review_is_scored(client, hr, job, spans, monkeypatch):  # noqa: F811
    calls = _spy(monkeypatch)

    unchanged = _draft(client, hr, job)
    client.post(f"/drafts/{unchanged['id']}/approve", headers=hr, json={"content": unchanged["content"]})
    edited = _draft(client, hr, job)
    client.post(f"/drafts/{edited['id']}/approve", headers=hr, json={"content": "HR rewrote this"})
    thrown_away = _draft(client, hr, job)
    client.post(f"/drafts/{thrown_away['id']}/discard", headers=hr)

    assert [(name, value) for _, name, value, _ in calls] == [
        ("hr_review", 1.0), ("hr_review", 0.5), ("hr_review", 0.0)]
    assert all(trace_id and len(trace_id) == 32 for trace_id, *_ in calls)
    assert "approved unchanged" in calls[0][3] and "discarded" in calls[2][3]


def test_review_works_with_tracing_off(client, hr, job):
    draft = _draft(client, hr, job)
    with SessionLocal() as db:
        assert db.get(Draft, draft["id"]).trace_id is None
    assert client.post(f"/drafts/{draft['id']}/approve", headers=hr, json={}).status_code == 200
