"""Langfuse tracing: on, off, privacy switch, and never breaking an agent run.

A real Langfuse client is used with an in-memory exporter, so these tests check what
would actually be sent without touching the network.
"""

import json
from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import observability
from app.config import settings
from app.llm import LLMError, OpenRouterLLM
from tests.test_agents import add


_exporter = InMemorySpanExporter()
_shared_client = None  # Langfuse keeps one client per key per process, so the tests share one.


@pytest.fixture
def spans(monkeypatch):
    """Turn tracing on for one test; yields a function returning the finished spans."""
    global _shared_client
    monkeypatch.setattr(settings, "langfuse_public_key", "pk-lf-test")
    monkeypatch.setattr(settings, "langfuse_secret_key", "sk-lf-test")
    monkeypatch.setattr(settings, "langfuse_capture_content", True)
    if _shared_client is None:
        observability.init_observability(span_exporter=_exporter)
        _shared_client = observability._client
    observability._client = _shared_client
    assert observability.enabled()
    observability.flush()
    _exporter.clear()

    def finished():
        observability.flush()
        return list(_exporter.get_finished_spans())

    yield finished
    observability.flush()
    observability._client = None  # tracing is off again for every other test


def _attrs(span) -> str:
    return json.dumps({k: str(v) for k, v in span.attributes.items()})


def _llm(answer='{"score": 70, "summary": "ok"}', usage=None, fail=False) -> OpenRouterLLM:
    class Completions:
        def create(self, **_):
            if fail:
                raise RuntimeError("provider is down")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=answer))], usage=usage)

    llm = OpenRouterLLM("test-key", ["model-a"])
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    return llm


def test_tracing_is_off_without_keys():
    assert not observability.enabled()
    with observability.trace("screening", "screen", "hr@example.com") as run:
        run.record(output="nothing is sent")          # no-op, must not raise
    with observability.generation("m", [{"role": "user", "content": "x"}]) as gen:
        gen.record(output="x", usage={"input": 1})


def test_agent_run_becomes_a_trace(client, hr, job, spans):
    add(client, hr, job["id"], "Asha Rao", "Python FastAPI")
    r = client.post("/agents/screening/screen", headers=hr, json={"job_id": job["id"]})
    assert r.status_code == 200, r.text

    runs = [s for s in spans() if s.name == "screening.screen"]
    assert len(runs) == 1
    sent = _attrs(runs[0])
    assert "hr@example.com" in sent          # who ran it
    assert "hiring-desk" in sent             # tags
    assert "candidate:read" in sent          # scopes used, as metadata
    assert "Screened 1 of 1" in sent         # the run's summary


def test_model_call_is_nested_under_the_agent_run(spans):
    usage = SimpleNamespace(prompt_tokens=120, completion_tokens=30)
    with observability.trace("screening", "screen", "hr@example.com"):
        _llm(usage=usage).complete("system rules", "RESUME: secret-resume-text", json_mode=True)

    by_name = {s.name: s for s in spans()}
    run, call = by_name["screening.screen"], by_name["llm.chat"]
    assert call.parent is not None and call.parent.span_id == run.context.span_id
    assert call.context.trace_id == run.context.trace_id
    sent = _attrs(call)
    assert "model-a" in sent and "120" in sent and "30" in sent
    assert "secret-resume-text" in sent      # content capture is on by default


def test_capture_content_off_sends_no_prompt_or_answer(spans, monkeypatch):
    monkeypatch.setattr(settings, "langfuse_capture_content", False)
    with observability.trace("writing", "offer", "hr@example.com", {"notes": "secret-note"}) as run:
        _llm(answer="secret-answer-text").complete("system rules", "RESUME: secret-resume-text")
        run.record(output="Drafted offer for secret-name")

    everything = " ".join(_attrs(s) for s in spans())
    for secret in ("secret-resume-text", "secret-answer-text", "secret-note", "secret-name", "system rules"):
        assert secret not in everything
    assert "model-a" in everything           # timings and model still recorded


def test_failed_model_call_is_marked_as_an_error(spans):
    with pytest.raises(LLMError):
        _llm(fail=True).complete("s", "p")
    call = next(s for s in spans() if s.name == "llm.chat")
    assert "ERROR" in _attrs(call) and "provider is down" in _attrs(call)


def test_agent_error_still_reaches_the_caller(client, hr, spans):
    r = client.post("/agents/screening/screen", headers=hr, json={"job_id": 999})
    assert r.status_code == 404
    run = next(s for s in spans() if s.name == "screening.screen")
    assert "ERROR" in _attrs(run)


def test_a_broken_langfuse_never_breaks_an_agent(client, hr, job, monkeypatch):
    class Broken:
        def start_as_current_observation(self, **_):
            raise RuntimeError("langfuse is down")

    monkeypatch.setattr(observability, "_client", Broken())
    add(client, hr, job["id"], "Asha Rao", "Python")
    r = client.post("/agents/screening/screen", headers=hr, json={"job_id": job["id"]})
    assert r.status_code == 200 and r.json()["summary"].startswith("Screened 1 of 1")


def test_screening_threads_keep_the_trace(client, hr, job, spans, monkeypatch):
    """Screening calls the model from a thread pool; those calls must stay in the run's trace."""
    from app.llm import get_llm_optional
    from app.main import app

    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    monkeypatch.setitem(app.dependency_overrides, get_llm_optional, lambda: _llm(usage=usage))
    for i in range(3):
        add(client, hr, job["id"], f"Person {i}", f"Python resume {i}")
    r = client.post("/agents/screening/screen", headers=hr, json={"job_id": job["id"]})
    assert r.status_code == 200, r.text

    all_spans = spans()
    run = next(s for s in all_spans if s.name == "screening.screen")
    calls = [s for s in all_spans if s.name == "llm.chat"]
    assert len(calls) == 3
    assert all(c.context.trace_id == run.context.trace_id for c in calls)


def test_runs_for_one_job_share_a_session(client, hr, interviewer, job, spans):
    """Sessions page: every agent run about the same job opening is grouped as job-<id>."""
    cid = add(client, hr, job["id"], "Asha Rao", "Python FastAPI")
    client.post("/agents/screening/screen", headers=hr, json={"job_id": job["id"]})            # by job
    client.post("/agents/writing/rejection", headers=hr, json={"candidate_id": cid})            # by candidate
    iid = client.post("/agents/scheduling/propose", headers=hr, json={
        "candidate_id": cid, "interviewer_id": interviewer["id"], "slots": ["2030-01-01T10:00"]}).json()["interview_id"]
    client.post("/agents/interview_kit/prepare", headers=hr, json={"interview_id": iid})        # by interview
    client.post("/agents/feedback/chase", headers=hr, json={})                                  # spans jobs

    runs = {s.name: _attrs(s) for s in spans() if "." in s.name and not s.name.startswith("llm")}
    expected = f"job-{job['id']}"
    for name in ("screening.screen", "writing.rejection", "scheduling.propose", "interview_kit.prepare"):
        assert expected in runs[name], name
    assert "job-" not in runs["feedback.chase"]


def test_session_lookup_survives_bad_parameters(client, hr, spans):
    r = client.post("/agents/writing/rejection", headers=hr, json={"candidate_id": "not-a-number"})
    assert r.status_code in (400, 404, 422)
