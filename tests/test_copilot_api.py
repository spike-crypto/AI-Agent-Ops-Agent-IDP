"""Chat API: background turns, live steps, approval cards, and who may see what."""

import pytest

from app import jobqueue, observability
from app.db import SessionLocal
from app.models import Candidate
from tests.stub_llm import ScriptedLLM
from tests.test_agents import add


@pytest.fixture
def planner(monkeypatch):
    """Install a scripted planner for the background worker; returns a setter."""
    holder = {"llm": ScriptedLLM([])}
    monkeypatch.setattr(jobqueue, "get_llm", lambda: holder["llm"])

    def use(answers):
        holder["llm"] = ScriptedLLM(answers)
    return use


def _chat(client, hr) -> int:
    r = client.post("/copilot/conversations", headers=hr, json={})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _say(client, hr, conv, text):
    r = client.post(f"/copilot/conversations/{conv}/messages", headers=hr, json={"text": text})
    assert r.status_code == 202, r.text
    while jobqueue.run_once():
        pass
    return r.json()


def test_full_turn_runs_in_the_background(client, hr, job, planner):
    add(client, hr, job["id"], "Asha Rao", "Python FastAPI")
    planner([{"tool": "find_job", "args": {"name": "backend"}},
             {"tool": "screen", "args": {"job_id": job["id"]}},
             {"answer": "Asha Rao scored 50."}])
    conv = _chat(client, hr)
    sent = _say(client, hr, conv, "Screen the new resumes for Backend")
    job_row = client.get(f"/agent-jobs/{sent['job_id']}", headers=hr).json()
    assert job_row["status"] == "done" and [s["tool"] for s in job_row["steps"]] == ["find_job", "screen"]

    thread = client.get(f"/copilot/conversations/{conv}", headers=hr).json()
    assert [m["role"] for m in thread["messages"]] == ["user", "assistant"]
    assert thread["messages"][1]["content"] == "Asha Rao scored 50."
    assert thread["title"].startswith("Screen the new resumes")
    listed = client.get("/copilot/conversations", headers=hr).json()
    assert listed[0]["id"] == conv


def test_approval_applies_the_card_once(client, hr, job, planner):
    cid = add(client, hr, job["id"], "Rahul Menon", "Python")
    planner([{"tool": "propose_stage_change", "args": {"candidate_id": cid, "stage": "rejected"}},
             {"tool": "propose_rejection_email", "args": {"candidate_id": cid}},
             {"answer": "2 changes are waiting for your approval."}])
    conv = _chat(client, hr)
    _say(client, hr, conv, "Reject Rahul and draft the email")
    proposal = client.get(f"/copilot/conversations/{conv}", headers=hr).json()["messages"][1]["proposal"]
    assert proposal["status"] == "pending" and len(proposal["items"]) == 2
    with SessionLocal() as db:
        assert db.get(Candidate, cid).status == "new"            # nothing yet

    first = client.post(f"/copilot/proposals/{proposal['id']}/approve", headers=hr)
    second = client.post(f"/copilot/proposals/{proposal['id']}/approve", headers=hr)
    assert first.status_code == 202 and second.status_code == 409
    while jobqueue.run_once():
        pass
    with SessionLocal() as db:
        assert db.get(Candidate, cid).status == "rejected"
    drafts = client.get("/drafts", headers=hr).json()
    assert [d["kind"] for d in drafts] == ["rejection"]          # applied exactly once
    thread = client.get(f"/copilot/conversations/{conv}", headers=hr).json()
    assert thread["messages"][-1]["content"].startswith("Done: 2 of 2")
    assert thread["messages"][1]["proposal"]["status"] == "done"


def test_dismiss_changes_nothing(client, hr, job, planner):
    cid = add(client, hr, job["id"], "Rahul Menon", "Python")
    planner([{"tool": "propose_stage_change", "args": {"candidate_id": cid, "stage": "rejected"}},
             {"answer": "Waiting for approval."}])
    conv = _chat(client, hr)
    _say(client, hr, conv, "Reject Rahul")
    pid = client.get(f"/copilot/conversations/{conv}", headers=hr).json()["messages"][1]["proposal"]["id"]
    assert client.post(f"/copilot/proposals/{pid}/dismiss", headers=hr).status_code == 200
    assert client.post(f"/copilot/proposals/{pid}/approve", headers=hr).status_code == 409
    while jobqueue.run_once():
        pass
    with SessionLocal() as db:
        assert db.get(Candidate, cid).status == "new"


def test_interviewers_cannot_use_the_copilot(client, hr, interviewer):
    h = interviewer["headers"]
    assert client.post("/copilot/conversations", headers=h, json={}).status_code == 403
    conv = _chat(client, hr)
    assert client.get(f"/copilot/conversations/{conv}", headers=h).status_code == 403


def test_conversations_are_private_to_their_hr_user(client, hr, planner):
    conv = _chat(client, hr)
    client.post("/users", headers=hr, json={"email": "hr2@example.com", "name": "Second HR",
                                            "password": "hr2-password-1", "role": "hr_admin"})
    from tests.conftest import login
    other = login(client, "hr2@example.com", "hr2-password-1")
    assert client.get(f"/copilot/conversations/{conv}", headers=other).status_code == 404
    assert client.post(f"/copilot/conversations/{conv}/messages", headers=other,
                       json={"text": "hi"}).status_code == 404
    assert client.get("/copilot/conversations", headers=other).json() == []


def test_message_length_is_limited(client, hr):
    conv = _chat(client, hr)
    assert client.post(f"/copilot/conversations/{conv}/messages", headers=hr,
                       json={"text": "x" * 2001}).status_code == 422
    assert client.post(f"/copilot/conversations/{conv}/messages", headers=hr,
                       json={"text": "   "}).status_code == 422


def test_approval_is_scored_in_langfuse(client, hr, job, planner, monkeypatch):
    scores = []
    monkeypatch.setattr(observability, "score", lambda tid, name, value, comment="": scores.append((name, value)))
    cid = add(client, hr, job["id"], "Rahul Menon", "Python")
    planner([{"tool": "propose_stage_change", "args": {"candidate_id": cid, "stage": "rejected"}},
             {"answer": "Waiting."}])
    conv = _chat(client, hr)
    _say(client, hr, conv, "Reject Rahul")
    pid = client.get(f"/copilot/conversations/{conv}", headers=hr).json()["messages"][1]["proposal"]["id"]
    client.post(f"/copilot/proposals/{pid}/dismiss", headers=hr)
    assert ("copilot_action", 0.0) in scores


def test_copilot_runs_share_a_session_per_conversation():
    from app.agents.runner import session_for

    with SessionLocal() as db:
        assert session_for(db, {"conversation_id": 7, "message_id": 3}) == "copilot-7"
