"""The provider loop: a bad answer from one free model must not fail the request."""

from types import SimpleNamespace

from app.llm import LLMError, OpenRouterLLM, parse_json


def _fake_client(answers: dict[str, str], calls: list[str]) -> SimpleNamespace:
    class Completions:
        def create(self, model, **_):
            calls.append(model)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=answers[model]))],
                usage=None,
            )

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def test_json_mode_fails_over_to_the_next_model_when_one_answers_in_prose():
    calls: list[str] = []
    llm = OpenRouterLLM("test-key", ["prose-model", "json-model"])
    llm._client = _fake_client(
        {
            "prose-model": "I'm sorry, I can only describe the candidate in words.",
            "json-model": '{"score": 88, "summary": "strong fit"}',
        },
        calls,
    )

    answer = llm.complete("system", "prompt", json_mode=True)

    assert calls == ["prose-model", "json-model"]
    assert parse_json(answer)["score"] == 88


def test_json_mode_accepts_an_object_wrapped_in_prose_or_a_fence():
    calls: list[str] = []
    llm = OpenRouterLLM("test-key", ["chatty-model"])
    llm._client = _fake_client(
        {"chatty-model": 'Here you go:\n```json\n{"ok": true}\n```\nHope that helps!'}, calls
    )

    assert parse_json(llm.complete("system", "prompt", json_mode=True))["ok"] is True


def test_plain_text_answers_are_not_held_to_json():
    calls: list[str] = []
    llm = OpenRouterLLM("test-key", ["writer"])
    llm._client = _fake_client({"writer": "Dear candidate, thank you."}, calls)

    assert llm.complete("system", "prompt").startswith("Dear candidate")


def test_all_models_failing_raises_one_readable_error():
    calls: list[str] = []
    llm = OpenRouterLLM("test-key", ["a", "b"])
    llm._client = _fake_client({"a": "", "b": "no json here"}, calls)

    try:
        llm.complete("system", "prompt", json_mode=True)
    except LLMError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected LLMError")

    assert calls == ["a", "b"]
    assert "All models failed" in message
    assert "a:" in message and "b:" in message
