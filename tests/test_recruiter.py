"""The Recruiter agent: the only agent allowed to move a candidate between hiring stages."""

import pytest

from app.agents.base import AgentContext, AgentPermissionError
from app.agents.registry import AGENTS
from app.db import SessionLocal
from app.models import Candidate, User
from tests.stub_llm import StubLLM
from tests.test_agents import add


def test_recruiter_moves_a_candidate(client, hr, job):
    cid = add(client, hr, job["id"], "Asha Rao", "Python")
    r = client.post("/agents/recruiter/set_stage", headers=hr, json={"candidate_id": cid, "stage": "shortlisted"})
    assert r.status_code == 200, r.text
    assert r.json()["from"] == "new" and r.json()["to"] == "shortlisted" and r.json()["candidate_id"] == cid
    with SessionLocal() as db:
        assert db.get(Candidate, cid).status == "shortlisted"


def test_unknown_stage_is_refused(client, hr, job):
    cid = add(client, hr, job["id"], "Asha Rao", "Python")
    r = client.post("/agents/recruiter/set_stage", headers=hr, json={"candidate_id": cid, "stage": "hired-yesterday"})
    assert r.status_code == 400


def test_interviewer_cannot_use_the_recruiter(client, hr, interviewer, job):
    cid = add(client, hr, job["id"], "Asha Rao", "Python")
    r = client.post("/agents/recruiter/set_stage", headers=interviewer["headers"],
                    json={"candidate_id": cid, "stage": "rejected"})
    assert r.status_code == 403


def test_no_other_agent_can_change_a_stage(client, hr, job):
    cid = add(client, hr, job["id"], "Asha Rao", "Python")
    with SessionLocal() as db:
        user = db.query(User).filter_by(role="hr_admin").first()
        for name, agent in AGENTS.items():
            if name == "recruiter":
                continue
            ctx = AgentContext(db, user, StubLLM(), name, agent.scopes)
            with pytest.raises(AgentPermissionError):
                ctx.set_stage(db.get(Candidate, cid), "rejected")


def test_the_copilot_carries_write_scopes_only_to_hand_on():
    copilot = AGENTS["copilot"]
    assert "candidate:stage:write" not in copilot.scopes
    assert "candidate:stage:write" in copilot.delegable_scopes
    assert all(s.endswith(":read") for s in copilot.scopes)
