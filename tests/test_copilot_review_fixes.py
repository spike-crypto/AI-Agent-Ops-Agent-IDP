"""Fixes from the final review of the HR Copilot branch."""

import time

import pytest

from app import copilot_tools as tools
from app import identity, jobqueue
from app.db import SessionLocal
from app.llm import LLMError
from app.models import ActionProposal, AgentJob, Candidate, Draft, Message, User
from tests.stub_llm import ScriptedLLM
from tests.test_agents import add
from tests.test_copilot_api import _chat, _say, planner  # noqa: F401  (fixture)


def _hr(db) -> User:
    return db.query(User).filter_by(email="hr@example.com").one()


# ---- 1. a failed turn must still leave a reply, so the chat never waits forever ---------------

def test_a_turn_that_cannot_start_still_answers(client, hr, monkeypatch):
    def no_model():
        raise LLMError("OPENROUTER_API_KEY is not set")
    monkeypatch.setattr(jobqueue, "get_llm", no_model)
    conv = _chat(client, hr)
    sent = _say(client, hr, conv, "Hello")
    assert client.get(f"/agent-jobs/{sent['job_id']}", headers=hr).json()["status"] == "error"
    last = client.get(f"/copilot/conversations/{conv}", headers=hr).json()["messages"][-1]
    assert last["role"] == "assistant" and last["content"].startswith("Copilot stopped")


def test_an_unexpected_agent_crash_is_a_failed_step(client, hr, job, planner, monkeypatch):  # noqa: F811
    add(client, hr, job["id"], "Asha Rao", "Python")
    from app.agents.screening import ScreeningAgent

    def boom(*_a, **_k):
        raise RuntimeError("database went away")
    monkeypatch.setattr(ScreeningAgent, "screen", boom)
    planner([{"tool": "screen", "args": {"job_id": job["id"]}}, {"answer": "Screening failed."}])
    conv = _chat(client, hr)
    _say(client, hr, conv, "Screen them")
    reply = client.get(f"/copilot/conversations/{conv}", headers=hr).json()["messages"][-1]
    assert reply["content"] == "Screening failed."
    assert reply["steps"][0]["ok"] is False and "database went away" in reply["steps"][0]["summary"]


# ---- 2. an exchanged token never outlives, or comes from, a spent or non-orchestrator token -----

def test_child_token_never_outlives_its_parent(monkeypatch):
    with SessionLocal() as db:
        parent = identity.issue_for_user(db, _hr(db), "copilot", tools.COPILOT_SCOPES, {})
        real = time.time
        monkeypatch.setattr(identity.time, "time", lambda: real() + identity.TOKEN_SECONDS - 10)
        child = identity.exchange(db, parent, "comparison", {"job:read", "candidate:read"})
        monkeypatch.setattr(identity.time, "time", lambda: real() + identity.TOKEN_SECONDS + 1)
        with pytest.raises(identity.TokenError, match="expired"):
            identity.verify(db, child, identity.agent_id("comparison"))


def test_a_spent_token_cannot_mint_new_ones():
    with SessionLocal() as db:
        parent = identity.issue_for_user(db, _hr(db), "copilot", tools.COPILOT_SCOPES, {})
        identity.verify(db, parent, identity.agent_id("copilot"))          # spends it
        with pytest.raises(identity.TokenError, match="already used"):
            identity.exchange(db, parent, "comparison", {"job:read"})


def test_only_an_orchestrator_token_can_be_exchanged():
    with SessionLocal() as db:
        worker = identity.issue_for_user(db, _hr(db), "screening", {"job:read", "candidate:read"}, {})
        with pytest.raises(identity.TokenError, match="cannot hand on"):
            identity.exchange(db, worker, "comparison", {"job:read"})


# ---- 3. applying a card is claimed atomically and survives a crash midway --------------------

def _card(client, hr, job, planner, items):  # noqa: F811
    planner(items + [{"answer": "Waiting for approval."}])
    conv = _chat(client, hr)
    _say(client, hr, conv, "Do it")
    pid = client.get(f"/copilot/conversations/{conv}", headers=hr).json()["messages"][1]["proposal"]["id"]
    return conv, pid


def test_a_card_is_applied_by_one_job_only(client, hr, job, planner):  # noqa: F811
    cid = add(client, hr, job["id"], "Rahul Menon", "Python")
    conv, pid = _card(client, hr, job, planner,
                      [{"tool": "propose_rejection_email", "args": {"candidate_id": cid}}])
    client.post(f"/copilot/proposals/{pid}/approve", headers=hr)
    with SessionLocal() as db:                       # a second approve job sneaks in (2 workers)
        jobqueue.enqueue(db, _hr(db), "copilot", "approve", {"proposal_id": pid})
    while jobqueue.run_once():
        pass
    assert client.get("/drafts", headers=hr).json().__len__() == 1


def test_a_crash_midway_marks_the_card_failed_and_says_so(client, hr, job, planner, monkeypatch):  # noqa: F811
    cid = add(client, hr, job["id"], "Rahul Menon", "Python")
    conv, pid = _card(client, hr, job, planner, [
        {"tool": "propose_rejection_email", "args": {"candidate_id": cid}},
        {"tool": "propose_stage_change", "args": {"candidate_id": cid, "stage": "rejected"}}])
    real_apply = tools.apply_item
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("worker died")
        return real_apply(*a, **k)
    monkeypatch.setattr("app.agents.copilot.apply_item", flaky)
    client.post(f"/copilot/proposals/{pid}/approve", headers=hr)
    while jobqueue.run_once():
        pass
    with SessionLocal() as db:
        card = db.get(ActionProposal, pid)
        assert card.status == "failed"
        assert [s["ok"] for s in card.result["steps"]] == [True, False]
        assert db.query(Draft).count() == 1 and db.get(Candidate, cid).status == "new"
    last = client.get(f"/copilot/conversations/{conv}", headers=hr).json()["messages"][-1]
    assert last["content"].startswith("Done: 1 of 2")


# ---- 4 and 5. arguments are checked, and interview cards show the real times -----------------

def test_a_string_is_not_split_into_a_list(client, hr, job):
    with SessionLocal() as db:
        token = identity.issue_for_user(db, _hr(db), "copilot", tools.COPILOT_SCOPES, {})
        r = tools.run_tool(db, _hr(db), ScriptedLLM([]), token, "compare", {"candidate_ids": "345"})
    assert not r.ok and "list" in r.summary


def test_interview_card_lists_the_times_and_checks_them(client, hr, interviewer, job):
    cid = add(client, hr, job["id"], "Rahul Menon", "Python")
    with SessionLocal() as db:
        token = identity.issue_for_user(db, _hr(db), "copilot", tools.COPILOT_SCOPES, {})
        ok = tools.run_tool(db, _hr(db), ScriptedLLM([]), token, "propose_interview", {
            "candidate_id": cid, "interviewer_id": interviewer["id"], "slots": ["2030-01-02T10:00"]})
        bad = tools.run_tool(db, _hr(db), ScriptedLLM([]), token, "propose_interview", {
            "candidate_id": cid, "interviewer_id": interviewer["id"], "slots": ["next tuesday"]})
    assert ok.ok and "2 Jan 2030, 10:00" in ok.proposal_item["label"]
    assert not bad.ok and "date" in bad.summary.lower()


# ---- 6. other HR admins cannot read someone's Copilot turns through the jobs API --------------

def test_copilot_jobs_are_private_to_their_owner(client, hr, planner):  # noqa: F811
    planner([{"answer": "Private answer."}])
    conv = _chat(client, hr)
    sent = _say(client, hr, conv, "Something private")
    client.post("/users", headers=hr, json={"email": "hr2@example.com", "name": "Second HR",
                                            "password": "hr2-password-1", "role": "hr_admin"})
    from tests.conftest import login
    other = login(client, "hr2@example.com", "hr2-password-1")
    assert client.get(f"/agent-jobs/{sent['job_id']}", headers=other).status_code == 404
    assert all(j["agent"] != "copilot" for j in client.get("/agent-jobs", headers=other).json())


# ---- 7. screening from chat fits inside one turn -----------------------------------------------

def test_chat_screening_is_small_enough_for_one_turn():
    from app.agents.copilot import MAX_TURN_SECONDS
    assert tools.CHAT_SCREEN_LIMIT * 40 / 3 < MAX_TURN_SECONDS / 2   # ~40 s per resume, 3 at a time
    assert tools.CHAT_SCREEN_LIMIT <= 4
