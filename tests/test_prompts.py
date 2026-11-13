"""Prompt versioning: every prompt is named and versioned, and the version must be
bumped when the text changes."""

from types import SimpleNamespace

import pytest

from app import observability, prompts
from app.llm import OpenRouterLLM
from app.prompts import Prompt


def test_prompts_match_the_lock_file():
    issues = prompts.problems()
    assert not issues, (
        "Prompt text and prompts.lock.json disagree:\n  " + "\n  ".join(issues)
        + "\nBump the version of any prompt you changed, then run: python -m app.prompts --write-lock"
    )


def test_every_agent_prompt_is_versioned():
    names = {p.name for p in prompts.all_prompts()}
    assert {"screening.system", "writing.system", "scheduling.system", "comparison.system",
            "interview_kit.system", "feedback.reminder", "feedback.summary",
            "feedback.decision_memo"} <= names
    assert all(p.version >= 1 for p in prompts.all_prompts())


def test_a_prompt_behaves_like_its_text():
    p = Prompt("test.only", 3, "You are a test.")
    try:
        assert p == "You are a test." and "test" in p and p.label == "test.only@v3"
    finally:
        prompts.REGISTRY.pop("test.only", None)


def test_changed_text_without_a_version_bump_is_caught(monkeypatch):
    locked = prompts.read_lock()
    locked["screening.system"] = {**locked["screening.system"], "sha256": "0" * 64}
    monkeypatch.setattr(prompts, "read_lock", lambda: locked)
    assert any("screening.system" in i and "bump it" in i for i in prompts.problems())


def test_two_different_prompts_cannot_share_a_name():
    prompts.all_prompts()
    with pytest.raises(ValueError):
        Prompt("screening.system", 1, "something else entirely")


def test_prompts_endpoint_is_hr_only(client, hr, interviewer):
    rows = client.get("/prompts", headers=hr).json()
    screening = next(r for r in rows if r["name"] == "screening.system")
    assert screening["version"] == 1 and screening["label"] == "screening.system@v1"
    assert client.get("/prompts", headers=interviewer["headers"]).status_code == 403


def test_model_call_carries_the_prompt_version(monkeypatch):
    seen = {}

    class FakeGeneration:
        def __enter__(self):
            return observability.Handle()

        def __exit__(self, *_):
            return False

    def fake_generation(model, messages, params=None, metadata=None):
        seen.update(metadata or {})
        return FakeGeneration()

    monkeypatch.setattr(observability, "generation", fake_generation)
    llm = OpenRouterLLM("test-key", ["m"])
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **_: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], usage=None))))
    from app.agents.screening import SYSTEM

    llm.complete(SYSTEM, "prompt")
    assert seen == {"prompt_name": "screening.system", "prompt_version": 1}
