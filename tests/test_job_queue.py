from app import jobqueue
from app.db import SessionLocal
from app.llm import LLMError
from app.models import AgentJob, Candidate, User
from tests.stub_llm import StubLLM
from tests.test_agents import add


def _queue(client, hr, job, **overrides):
    body = {"agent": "screening", "action": "screen", "params": {"job_id": job["id"]}}
    body.update(overrides)
    return client.post("/agent-jobs", headers=hr, json=body)


def _job_out(client, hr, job_id):
    return client.get(f"/agent-jobs/{job_id}", headers=hr).json()


def test_queued_screening_runs_in_the_background(client, hr, job, monkeypatch):
    for i in range(3):
        add(client, hr, job["id"], f"Person {i}", f"Python FastAPI Docker resume {i}")
    stub = StubLLM()
    monkeypatch.setattr(jobqueue, "get_llm", lambda: stub)

    r = _queue(client, hr, job)
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    assert r.json()["status"] == "queued"
    assert r.json()["progress_current"] == 0 and r.json()["result"] is None

    assert jobqueue.run_once() is True
    done = _job_out(client, hr, job_id)
    assert done["status"] == "done"
    assert done["progress_current"] == done["progress_total"] == 3
    assert done["started_at"] and done["finished_at"]
    assert done["result"]["processed"] == 3 and done["result"]["remaining"] == 0
    assert len(stub.calls) == 3
    assert all(c["status"] == "screened"
               for c in client.get(f"/jobs/{job['id']}/candidates", headers=hr).json())
    run = client.get("/agent-runs", headers=hr).json()[0]
    assert run["agent"] == "screening" and run["status"] == "success"


def test_worker_does_not_pick_up_finished_jobs_again(client, hr, job, monkeypatch):
    add(client, hr, job["id"], "Once Only", "Python")
    stub = StubLLM()
    monkeypatch.setattr(jobqueue, "get_llm", lambda: stub)
    job_id = _queue(client, hr, job).json()["id"]
    assert jobqueue.run_once() is True
    assert jobqueue.run_once() is False
    assert _job_out(client, hr, job_id)["status"] == "done"
    assert len(stub.calls) == 1


def test_queue_validates_agent_action_role_and_params(client, hr, interviewer, job):
    assert _queue(client, hr, job, agent="nope").status_code == 404
    assert _queue(client, hr, job, action="nope").status_code == 400
    assert _queue(client, hr, job, params=["not", "an", "object"]).status_code == 400
    assert _queue(client, interviewer["headers"], job).status_code == 403
    # interview_kit is HR-only, so HR may queue it too.
    r = client.post("/agent-jobs", headers=hr, json={"agent": "interview_kit", "action": "prepare",
                                                    "params": {"interview_id": 1}})
    assert r.status_code == 202
    assert client.post("/agent-jobs", headers=interviewer["headers"], json={
        "agent": "interview_kit", "action": "prepare", "params": {"interview_id": 1}}).status_code == 403


def test_missing_ai_provider_fails_the_job_with_a_clear_error(client, hr, job, monkeypatch):
    def no_provider():
        raise LLMError("OPENROUTER_API_KEY is not set")

    monkeypatch.setattr(jobqueue, "get_llm", no_provider)
    add(client, hr, job["id"], "Someone", "Python")
    job_id = _queue(client, hr, job).json()["id"]
    assert jobqueue.run_once() is True
    out = _job_out(client, hr, job_id)
    assert out["status"] == "error"
    assert "OPENROUTER_API_KEY" in out["error"]
    assert out["finished_at"]


def test_cancelling_a_queued_job_means_it_never_runs(client, hr, job, monkeypatch):
    add(client, hr, job["id"], "Never Screened", "Python")
    stub = StubLLM()
    monkeypatch.setattr(jobqueue, "get_llm", lambda: stub)
    job_id = _queue(client, hr, job).json()["id"]

    r = client.post(f"/agent-jobs/{job_id}/cancel", headers=hr)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert jobqueue.run_once() is False
    assert len(stub.calls) == 0
    assert _job_out(client, hr, job_id)["status"] == "cancelled"


def test_cancelling_mid_run_stops_after_the_current_resume(client, hr, job, monkeypatch):
    for i in range(4):
        add(client, hr, job["id"], f"Person {i}", f"Python Docker {i}")
    job_id = _queue(client, hr, job).json()["id"]

    stopped = {"now": False}

    class CancelAfterFirstAnswer(StubLLM):
        def complete(self, system, prompt, json_mode=False):
            out = super().complete(system, prompt, json_mode)
            stopped["now"] = True
            return out

    monkeypatch.setattr(jobqueue, "get_llm", lambda: CancelAfterFirstAnswer())
    monkeypatch.setattr(jobqueue, "_is_cancelled", lambda _job_id: stopped["now"])
    assert jobqueue.run_once() is True

    out = _job_out(client, hr, job_id)
    assert out["status"] == "cancelled"
    assert out["result"]["cancelled"] is True
    assert out["result"]["processed"] == 1               # the resume that was already in flight
    assert out["finished_at"]
    with SessionLocal() as db:
        screened = db.query(Candidate).filter_by(job_id=job["id"], status="screened").count()
        assert screened == 1                             # finished work survives the cancel
        assert db.query(Candidate).filter_by(job_id=job["id"], status="new").count() == 3


def test_a_job_from_a_deactivated_user_errors_instead_of_running(client, hr, job, monkeypatch):
    add(client, hr, job["id"], "Someone", "Python")
    stub = StubLLM()
    monkeypatch.setattr(jobqueue, "get_llm", lambda: stub)
    job_id = _queue(client, hr, job).json()["id"]
    with SessionLocal() as db:
        user = db.query(User).filter_by(email="hr@example.com").one()
        user.is_active = False
        db.commit()
    assert jobqueue.run_once() is True
    assert len(stub.calls) == 0
    with SessionLocal() as db:
        row = db.get(AgentJob, job_id)
        assert row.status == "error" and "no longer active" in row.error


def test_people_only_see_their_own_jobs_unless_they_are_hr(client, hr, interviewer, job):
    add(client, hr, job["id"], "Someone", "Python")
    job_id = _queue(client, hr, job).json()["id"]
    assert client.get(f"/agent-jobs/{job_id}", headers=interviewer["headers"]).status_code == 404
    assert client.get("/agent-jobs", headers=interviewer["headers"]).json() == []
    assert client.get(f"/agent-jobs/{job_id}", headers=hr).status_code == 200
    assert [j["id"] for j in client.get("/agent-jobs", headers=hr).json()] == [job_id]
    assert client.get("/agent-jobs/9999", headers=hr).status_code == 404


def test_cancel_is_scoped_to_the_owner(client, hr, interviewer, job):
    job_id = _queue(client, hr, job).json()["id"]
    assert client.post(f"/agent-jobs/{job_id}/cancel",
                       headers=interviewer["headers"]).status_code == 404
    assert _job_out(client, hr, job_id)["status"] == "queued"
