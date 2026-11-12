import pytest

from app.agents.base import AgentContext, AgentPermissionError
from app.agents.registry import AGENTS
from app.db import SessionLocal
from app.models import User
from tests.stub_llm import StubLLM


def add(client, hr, job_id, name, resume):
    r = client.post(f"/jobs/{job_id}/candidates", headers=hr,
                    json={"name": name, "email": f"{name.split()[0].lower()}@mail.test", "resume_text": resume})
    assert r.status_code == 201
    return r.json()["id"]


def test_screening_ranks_best_match_first(client, hr, job):
    add(client, hr, job["id"], "Weak Match", "I do graphic design and Photoshop.")
    add(client, hr, job["id"], "Strong Match", "Built Python FastAPI services, shipped with Docker on PostgreSQL.")
    r = client.post("/agents/screening/screen", headers=hr, json={"job_id": job["id"]})
    assert r.status_code == 200, r.text
    ranked = r.json()["ranked"]
    assert [c["name"] for c in ranked] == ["Strong Match", "Weak Match"]
    assert ranked[0]["score"] > ranked[1]["score"]
    assert all(c["status"] == "screened" for c in ranked)


def test_screening_agent_never_sees_salary(job):
    with SessionLocal() as db:
        hr = db.query(User).filter_by(role="hr_admin").one()
        agent = AGENTS["screening"]
        ctx = AgentContext(db, hr, StubLLM(), agent.name, agent.scopes)
        assert "salary_band" not in ctx.job(job["id"])
        with pytest.raises(AgentPermissionError):
            ctx.create_draft("offer", "x", "y")


def test_writing_agent_sees_salary_for_offer(client, hr, job):
    cid = add(client, hr, job["id"], "Asha Rao", "Python FastAPI")
    r = client.post("/agents/writing/offer", headers=hr, json={"candidate_id": cid})
    assert r.status_code == 200, r.text
    runs = client.get("/agent-runs", headers=hr).json()
    assert "job:salary:read" in runs[0]["scopes_used"]


def test_drafts_need_human_approval(client, hr, job):
    r = client.post("/agents/writing/job_description", headers=hr, json={"job_id": job["id"]})
    draft = r.json()["draft"]
    assert draft["status"] == "pending"
    assert client.get(f"/jobs/{job['id']}", headers=hr).json()["description"] == ""

    r = client.post(f"/drafts/{draft['id']}/approve", headers=hr, json={"content": "Edited by HR"})
    assert r.json()["status"] == "approved"
    assert client.get(f"/jobs/{job['id']}", headers=hr).json()["description"] == "Edited by HR"
    # Cannot approve twice.
    assert client.post(f"/drafts/{draft['id']}/approve", headers=hr, json={}).status_code == 409


def test_interviewer_cannot_run_agents_and_it_is_audited(client, hr, interviewer, job):
    r = client.post("/agents/screening/screen", headers=interviewer["headers"], json={"job_id": job["id"]})
    assert r.status_code == 403
    runs = client.get("/agent-runs", headers=hr).json()
    assert runs[0]["status"] == "denied"


def test_unknown_agent_or_action(client, hr):
    assert client.post("/agents/nope/run", headers=hr, json={}).status_code == 404
    assert client.post("/agents/screening/explode", headers=hr, json={}).status_code == 400
    assert client.post("/agents/screening/screen", headers=hr, json={"wrong": 1}).status_code == 400


def test_full_flow_schedule_feedback_summary(client, hr, interviewer, job):
    cid = add(client, hr, job["id"], "Ravi Kumar", "Python FastAPI Docker")
    # Interviewer can't see a candidate before being assigned.
    assert client.get(f"/candidates/{cid}", headers=interviewer["headers"]).status_code == 404

    r = client.post("/agents/scheduling/propose", headers=hr, json={
        "candidate_id": cid, "interviewer_id": interviewer["id"],
        "slots": ["2020-01-02T10:00", "2020-01-01T15:00"]})
    assert r.status_code == 200, r.text
    iid = r.json()["interview_id"]
    assert r.json()["draft"]["kind"] == "invite"

    # Now assigned: interviewer sees the candidate, but not the agent's score.
    seen = client.get(f"/candidates/{cid}", headers=interviewer["headers"]).json()
    assert "score" not in seen

    assert client.post(f"/interviews/{iid}/confirm", headers=hr,
                       json={"slot": "2020-01-01T15:00"}).status_code == 200

    # Past interview with no feedback -> feedback agent drafts a reminder.
    r = client.post("/agents/feedback/chase", headers=hr, json={})
    assert len(r.json()["drafts"]) == 1

    # HR is not the assigned interviewer, so HR can't submit feedback for this interview.
    assert client.post(f"/interviews/{iid}/feedback", headers=hr,
                       json={"rating": 4, "notes": "x"}).status_code == 404
    assert client.post(f"/interviews/{iid}/feedback", headers=interviewer["headers"],
                       json={"rating": 4, "notes": "Strong on APIs"}).status_code == 201

    r = client.post("/agents/feedback/summarize", headers=hr, json={"candidate_id": cid})
    assert r.status_code == 200
    assert r.json()["average_rating"] == 4.0
    assert client.post("/agents/feedback/chase", headers=hr, json={}).json()["drafts"] == []


def test_upload_resumes(client, hr, job):
    files = [("files", ("priya_sharma.txt", b"Python developer priya@mail.test", "text/plain")),
             ("files", ("photo.png", b"...", "image/png"))]
    r = client.post(f"/jobs/{job['id']}/candidates/upload", headers=hr, files=files)
    assert r.status_code == 201
    body = r.json()
    assert body["created"][0]["name"] == "Priya Sharma"
    assert body["created"][0]["email"] == "priya@mail.test"
    assert body["skipped"][0]["file"] == "photo.png"
