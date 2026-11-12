import pytest

from app.agents.base import AgentContext, AgentPermissionError
from app.agents.registry import AGENTS
from app.db import SessionLocal
from app.models import Draft, Interview, User
from tests.stub_llm import StubLLM


def create_interview_fixture(client, hr, interviewer, job):
    """Helper to add candidate and schedule an interview."""
    res_cand = client.post(
        f"/jobs/{job['id']}/candidates",
        headers=hr,
        json={
            "name": "Kavitha Raman",
            "email": "kavitha@mail.test",
            "resume_text": "Experienced Python Backend Developer with FastAPI, Docker, and PostgreSQL.",
        },
    )
    assert res_cand.status_code == 201
    cid = res_cand.json()["id"]

    # Propose interview
    res_iv = client.post(
        "/agents/scheduling/propose",
        headers=hr,
        json={
            "candidate_id": cid,
            "interviewer_id": interviewer["id"],
            "slots": ["2026-11-20T10:00:00", "2026-11-21T14:00:00"],
        },
    )
    assert res_iv.status_code == 200
    interview_id = res_iv.json()["interview_id"]
    return cid, interview_id


def test_hr_admin_can_generate_interview_kit(client, hr, interviewer, job):
    cid, iid = create_interview_fixture(client, hr, interviewer, job)

    r = client.post("/agents/interview_kit/prepare", headers=hr, json={"interview_id": iid})
    assert r.status_code == 200, r.text
    data = r.json()
    assert "summary" in data
    assert data["interview_id"] == iid
    assert "draft" in data

    draft = data["draft"]
    assert draft["kind"] == "interview_kit"
    assert draft["status"] == "pending"
    assert "Kavitha Raman" in draft["subject"]


def test_interview_kit_draft_created_and_linked_to_interview(client, hr, interviewer, job):
    cid, iid = create_interview_fixture(client, hr, interviewer, job)

    r = client.post("/agents/interview_kit/prepare", headers=hr, json={"interview_id": iid})
    assert r.status_code == 200
    draft_id = r.json()["draft"]["id"]

    # Verify directly in the DB that draft is properly persisted and linked
    with SessionLocal() as db:
        draft = db.get(Draft, draft_id)
        assert draft is not None
        assert draft.interview_id == iid
        assert draft.candidate_id == cid
        assert draft.job_id == job["id"]
        assert draft.status == "pending"
        assert draft.agent == "interview_kit"


def test_kit_contains_questions_and_rubric(client, hr, interviewer, job):
    cid, iid = create_interview_fixture(client, hr, interviewer, job)

    r = client.post("/agents/interview_kit/prepare", headers=hr, json={"interview_id": iid})
    assert r.status_code == 200
    content = r.json()["draft"]["content"]

    # Verify questions and rubric sections exist
    assert "Questions" in content or "Question" in content
    assert "Rubric" in content or "rubric" in content

    # Verify 6-8 questions exist
    questions = [line for line in content.splitlines() if line.strip() and line.strip()[0].isdigit() and "." in line[:4]]
    assert 6 <= len(questions) <= 8


def test_interview_kit_agent_never_receives_salary_band(job, hr, interviewer, client):
    cid, iid = create_interview_fixture(client, hr, interviewer, job)

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
        agent = AGENTS["interview_kit"]
        ctx = AgentContext(db, hr_user, spy_llm, agent.name, agent.scopes)

        # Context read verification
        job_data = ctx.job(job["id"])
        assert "salary_band" not in job_data

        # Execute prepare method
        result = agent.prepare(ctx, iid)
        assert result["draft"]["id"] is not None

        # Verify LLM prompt never received the salary band
        assert len(spy_llm.prompts) == 1
        assert "INR 12-16 LPA" not in spy_llm.prompts[0]
        assert "salary_band" not in spy_llm.prompts[0]


def test_interviewer_receives_http_403_and_denial_recorded(client, hr, interviewer, job):
    cid, iid = create_interview_fixture(client, hr, interviewer, job)

    # Interviewer role attempts to run the interview kit agent
    r = client.post("/agents/interview_kit/prepare", headers=interviewer["headers"], json={"interview_id": iid})
    assert r.status_code == 403

    # Check agent-runs audit log
    runs = client.get("/agent-runs", headers=hr).json()
    assert len(runs) > 0
    denied_run = runs[0]
    assert denied_run["agent"] == "interview_kit"
    assert denied_run["action"] == "prepare"
    assert denied_run["status"] == "denied"
    assert "Role 'interviewer' may not run agent 'interview_kit'" in denied_run["error"]


def test_unknown_interview_id_returns_404(client, hr):
    r = client.post("/agents/interview_kit/prepare", headers=hr, json={"interview_id": 99999})
    assert r.status_code == 404
    assert "Interview 99999 not found" in r.text
