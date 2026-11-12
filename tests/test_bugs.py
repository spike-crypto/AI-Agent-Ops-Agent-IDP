import pytest

from app import main
from app.db import SessionLocal
from app.llm import LLMError, parse_json
from app.models import Candidate, Interview, User
from tests.conftest import override_llm
from tests.stub_llm import StubLLM
from tests.test_agents import add


def test_rescreening_does_not_undo_a_human_decision(client, hr, job):
    cid = add(client, hr, job["id"], "Short Listed", "Python FastAPI")
    client.post("/agents/screening/screen", headers=hr, json={"job_id": job["id"]})
    client.post(f"/candidates/{cid}/status", headers=hr, json={"status": "shortlisted"})

    r = client.post("/agents/screening/screen", headers=hr,
                    json={"job_id": job["id"], "rescreen": True})
    assert r.status_code == 200, r.text
    assert client.get(f"/candidates/{cid}", headers=hr).json()["status"] == "shortlisted"


def test_cannot_confirm_a_finished_interview(client, hr, interviewer, job):
    cid = add(client, hr, job["id"], "Done Person", "Python")
    iid = client.post("/agents/scheduling/propose", headers=hr, json={
        "candidate_id": cid, "interviewer_id": interviewer["id"],
        "slots": ["2030-01-01T10:00"]}).json()["interview_id"]
    assert client.post(f"/interviews/{iid}/confirm", headers=hr,
                       json={"slot": "2030-01-01T10:00"}).status_code == 200
    assert client.post(f"/interviews/{iid}/feedback", headers=interviewer["headers"],
                       json={"rating": 4, "notes": "Good"}).status_code == 201

    r = client.post(f"/interviews/{iid}/confirm", headers=hr, json={"slot": "2030-01-01T10:00"})
    assert r.status_code == 409


def test_a_corrupt_stored_slot_returns_400_not_500(client, hr, interviewer, job):
    cid = add(client, hr, job["id"], "Odd Slot", "Python")
    with SessionLocal() as db:
        interview = Interview(candidate_id=cid, interviewer_id=interviewer["id"],
                              proposed_slots=["not-a-date"], status="proposed")
        db.add(interview)
        db.commit()
        iid = interview.id

    r = client.post(f"/interviews/{iid}/confirm", headers=hr, json={"slot": "not-a-date"})
    assert r.status_code == 400


def test_job_status_is_validated(client, hr, job):
    r = client.patch(f"/jobs/{job['id']}", headers=hr, json={"status": "banana"})
    assert r.status_code == 400
    assert client.patch(f"/jobs/{job['id']}", headers=hr,
                        json={"status": "closed"}).status_code == 200


def test_upload_file_cap(client, hr, job):
    files = [("files", (f"{i}.txt", b"Python developer", "text/plain")) for i in range(21)]
    r = client.post(f"/jobs/{job['id']}/candidates/upload", headers=hr, files=files)
    assert r.status_code == 400 and "20" in r.json()["detail"]


def test_agent_runs_rejects_bad_limits(client, hr):
    assert client.get("/agent-runs?limit=-1", headers=hr).status_code == 422
    assert client.get("/agent-runs?limit=9999", headers=hr).status_code == 422


def test_manual_candidate_email_is_validated(client, hr, job):
    r = client.post(f"/jobs/{job['id']}/candidates", headers=hr,
                    json={"name": "No Mail", "email": "broken@", "resume_text": "Python"})
    assert r.status_code == 400


def test_comparison_reports_when_the_model_fails(client, hr, job):
    a = add(client, hr, job["id"], "First Person", "Python")
    b = add(client, hr, job["id"], "Second Person", "Docker")
    override_llm(StubLLM(fail=True))
    r = client.post("/agents/comparison/compare", headers=hr, json={"candidate_ids": [a, b]})
    assert r.status_code == 200, r.text
    assert r.json()["ai_error"]
    assert r.json()["candidates"]  # scores still drive the ranking


def test_health_reports_database_llm_and_request_id(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["database"] == "ok"
    assert body["llm"]["configured"] is False
    assert r.headers["x-request-id"]


def test_request_id_is_echoed(client):
    r = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"


def test_live_ai_check_uses_the_real_dependency(client, hr):
    r = client.get("/health/ai", headers=hr)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_live_ai_check_says_so_when_not_configured(client, hr):
    from app.llm import get_llm_optional
    from app.main import app

    app.dependency_overrides.pop(get_llm_optional, None)
    r = client.get("/health/ai", headers=hr)
    assert r.status_code == 503
    assert "OPENROUTER_API_KEY" in r.json()["error"]


def test_ai_check_is_hr_only(client, interviewer):
    assert client.get("/health/ai", headers=interviewer["headers"]).status_code == 403


@pytest.mark.parametrize("text", [
    '{"score": 80}',
    '```json\n{"score": 80}\n```',
    'Here is the answer:\n{"score": 80}\nHope that helps.',
    '{"score": 80}{"other": 1}',
])
def test_parse_json_handles_model_noise(text):
    assert parse_json(text)["score"] == 80


def test_parse_json_rejects_non_json():
    with pytest.raises(LLMError):
        parse_json("no json here")
