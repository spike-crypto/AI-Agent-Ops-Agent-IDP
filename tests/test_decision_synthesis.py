import pytest

from app.agents.base import AgentContext
from app.agents.registry import AGENTS
from app.db import SessionLocal
from app.models import Draft, Feedback, Interview, User
from tests.stub_llm import StubLLM


def setup_interview_with_feedback(client, hr, interviewer, job):
    # 1. Add candidate
    r_cand = client.post(
        f"/jobs/{job['id']}/candidates",
        headers=hr,
        json={
            "name": "Arun Kumar",
            "email": "arun@mail.test",
            "resume_text": "Experienced Python Developer with 4 years building FastAPI and Docker services.",
        },
    )
    assert r_cand.status_code == 201
    cid = r_cand.json()["id"]

    # 2. Schedule interview
    r_iv = client.post(
        "/agents/scheduling/propose",
        headers=hr,
        json={
            "candidate_id": cid,
            "interviewer_id": interviewer["id"],
            "slots": ["2020-01-01T15:00"],
        },
    )
    assert r_iv.status_code == 200
    iid = r_iv.json()["interview_id"]

    # Confirm slot
    r_cf = client.post(f"/interviews/{iid}/confirm", headers=hr, json={"slot": "2020-01-01T15:00"})
    assert r_cf.status_code == 200

    # 3. Submit feedback
    r_fb = client.post(
        f"/interviews/{iid}/feedback",
        headers=interviewer["headers"],
        json={"rating": 5, "notes": "Exceptional understanding of FastAPI internals, async I/O, and microservices."},
    )
    assert r_fb.status_code == 201
    return cid, iid


def test_synthesize_decision_requires_feedback(client, hr, job):
    # Candidate without feedback should return 400
    r_cand = client.post(
        f"/jobs/{job['id']}/candidates",
        headers=hr,
        json={"name": "No Feedback Candidate", "email": "nofb@mail.test", "resume_text": "Python coder"},
    )
    cid = r_cand.json()["id"]

    r = client.post("/agents/feedback/synthesize", headers=hr, json={"candidate_id": cid})
    assert r.status_code == 400
    assert "No interview feedback submitted yet" in r.text


def test_hr_admin_can_synthesize_decision(client, hr, interviewer, job):
    cid, iid = setup_interview_with_feedback(client, hr, interviewer, job)

    r = client.post("/agents/feedback/synthesize", headers=hr, json={"candidate_id": cid})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "summary" in data
    assert data["average_rating"] == 5.0
    assert data["feedback_count"] == 1
    assert "draft" in data

    draft = data["draft"]
    assert draft["kind"] == "decision_memo"
    assert draft["status"] == "pending"
    assert "Arun Kumar" in draft["subject"]
    assert "Hiring Decision Recommendation" in draft["content"]


def test_decision_memo_persisted_in_db(client, hr, interviewer, job):
    cid, iid = setup_interview_with_feedback(client, hr, interviewer, job)

    r = client.post("/agents/feedback/synthesize", headers=hr, json={"candidate_id": cid})
    assert r.status_code == 200
    draft_id = r.json()["draft"]["id"]

    with SessionLocal() as db:
        d = db.get(Draft, draft_id)
        assert d is not None
        assert d.kind == "decision_memo"
        assert d.candidate_id == cid
        assert d.job_id == job["id"]
        assert d.status == "pending"
        assert d.agent == "feedback"


def test_interviewer_cannot_synthesize_decision(client, hr, interviewer, job):
    cid, iid = setup_interview_with_feedback(client, hr, interviewer, job)

    r = client.post("/agents/feedback/synthesize", headers=interviewer["headers"], json={"candidate_id": cid})
    assert r.status_code == 403

    # Check agent run audit record
    runs = client.get("/agent-runs", headers=hr).json()
    assert runs[0]["agent"] == "feedback"
    assert runs[0]["action"] == "synthesize"
    assert runs[0]["status"] == "denied"


def test_synthesis_never_sees_salary_band(job, hr, interviewer, client):
    cid, iid = setup_interview_with_feedback(client, hr, interviewer, job)

    class SpyLLM(StubLLM):
        def __init__(self):
            super().__init__()
            self.prompts: list[str] = []

        def complete(self, system: str, prompt: str, json_mode: bool = False) -> str:
            self.prompts.append(prompt)
            return super().complete(system, prompt, json_mode)

    spy_llm = SpyLLM()
    with SessionLocal() as db:
        hr_user = db.query(User).filter_by(role="hr_admin").one()
        agent = AGENTS["feedback"]
        ctx = AgentContext(db, hr_user, spy_llm, agent.name, agent.scopes)

        assert "salary_band" not in ctx.job(job["id"])
        agent.synthesize(ctx, cid)

        assert len(spy_llm.prompts) == 1
        assert "INR 12-16 LPA" not in spy_llm.prompts[0]
        assert "salary_band" not in spy_llm.prompts[0]
