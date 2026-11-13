"""The Copilot's tool catalog: read tools, delegated agent tools, and propose-only tools."""

from app import copilot_tools as tools
from app import identity
from app.db import SessionLocal
from app.models import AgentRun, Candidate, Draft, Interview, TokenGrant, User
from tests.stub_llm import StubLLM
from tests.test_agents import add


def _hr(db) -> User:
    return db.query(User).filter_by(email="hr@example.com").one()


def _token(db, user=None, scopes=None) -> str:
    user = user or _hr(db)
    return identity.issue_for_user(db, user, "copilot", scopes or tools.COPILOT_SCOPES, {"turn": 1})


def _run(name, args, scopes=None, user_email="hr@example.com"):
    with SessionLocal() as db:
        user = db.query(User).filter_by(email=user_email).one()
        return tools.run_tool(db, user, StubLLM(), _token(db, user, scopes), name, args)


def test_catalog_has_the_three_kinds():
    kinds = {t.name: t.kind for t in tools.TOOLS.values()}
    assert {n for n, k in kinds.items() if k == "read"} == {
        "list_jobs", "find_job", "list_candidates", "get_candidate", "list_interviews", "pending_drafts"}
    assert {n for n, k in kinds.items() if k == "delegate"} == {
        "screen", "compare", "summarize_feedback", "decision_memo", "interview_kit"}
    assert {n for n, k in kinds.items() if k == "propose"} == {
        "propose_stage_change", "propose_rejection_email", "propose_offer_draft", "propose_interview"}


def test_read_tools_never_return_resume_text(client, hr, job):
    add(client, hr, job["id"], "Asha Rao", "Python FastAPI SECRET-RESUME-TEXT")
    client.post("/agents/screening/screen", headers=hr, json={"job_id": job["id"]})
    for name, args in [("list_jobs", {}), ("find_job", {"name": "backend"}),
                       ("list_candidates", {"job_id": job["id"]}), ("get_candidate", {"candidate_id": 1}),
                       ("list_interviews", {}), ("pending_drafts", {})]:
        result = _run(name, args)
        assert result.ok, (name, result.summary)
        assert "SECRET-RESUME-TEXT" not in str(result.data) + result.summary, name


def test_find_job_and_ranked_candidates(client, hr, job):
    add(client, hr, job["id"], "Strong One", "Python FastAPI Docker PostgreSQL")
    add(client, hr, job["id"], "Weak One", "Photoshop")
    client.post("/agents/screening/screen", headers=hr, json={"job_id": job["id"]})
    found = _run("find_job", {"name": "BACKEND"})
    assert found.data["jobs"][0]["id"] == job["id"]
    ranked = _run("list_candidates", {"job_id": job["id"], "top": 1})
    assert [c["name"] for c in ranked.data["candidates"]] == ["Strong One"]
    assert "untrusted" in str(ranked.data)          # model-written summaries are marked


def test_read_tool_needs_its_scope_on_the_copilot_token(client, hr, job):
    result = _run("pending_drafts", {}, scopes={"job:read"})
    assert not result.ok and "draft:read" in result.summary


def test_delegate_tool_exchanges_a_token_and_runs_the_agent(client, hr, job):
    add(client, hr, job["id"], "Asha Rao", "Python FastAPI")
    result = _run("screen", {"job_id": job["id"]})
    assert result.ok, result.summary
    assert result.grant["audience"] == "agent:screening"
    assert result.grant["actors"] == ["agent:screening", "agent:copilot"]
    with SessionLocal() as db:
        run = db.query(AgentRun).filter_by(agent="screening").one()
        assert run.status == "success" and run.delegation["actors"] == ["agent:screening", "agent:copilot"]
        assert db.query(TokenGrant).filter_by(audience="agent:screening").one().used_at is not None
        assert db.query(Candidate).one().status == "screened"     # the exempt automatic move


def test_compare_returns_a_compact_summary(client, hr, job):
    a = add(client, hr, job["id"], "Strong One", "Python FastAPI Docker PostgreSQL")
    b = add(client, hr, job["id"], "Weak One", "Photoshop")
    result = _run("compare", {"candidate_ids": [a, b]})
    assert result.ok and [c["name"] for c in result.data["candidates"]][0] == "Strong One"


def test_propose_tools_change_nothing(client, hr, interviewer, job):
    cid = add(client, hr, job["id"], "Asha Rao", "Python")
    for name, args in [("propose_stage_change", {"candidate_id": cid, "stage": "rejected"}),
                       ("propose_rejection_email", {"candidate_id": cid}),
                       ("propose_offer_draft", {"candidate_id": cid}),
                       ("propose_interview", {"candidate_id": cid, "interviewer_id": interviewer["id"],
                                              "slots": ["2030-01-01T10:00"]})]:
        result = _run(name, args)
        assert result.ok and result.proposal_item, (name, result.summary)
        assert "Asha Rao" in result.proposal_item["label"]
    with SessionLocal() as db:
        assert db.get(Candidate, cid).status == "new"
        assert db.query(Draft).count() == 0 and db.query(Interview).count() == 0
        assert db.query(AgentRun).count() == 0


def test_proposed_stage_label_is_plain_english(client, hr, job):
    cid = add(client, hr, job["id"], "Rahul Menon", "Python")
    item = _run("propose_stage_change", {"candidate_id": cid, "stage": "rejected"}).proposal_item
    assert item["label"] == "Move Rahul Menon to Not moving forward"
    assert (item["agent"], item["action"]) == ("recruiter", "set_stage")


def test_apply_item_runs_the_approved_change_through_a_token(client, hr, job):
    cid = add(client, hr, job["id"], "Rahul Menon", "Python")
    item = _run("propose_stage_change", {"candidate_id": cid, "stage": "rejected"}).proposal_item
    with SessionLocal() as db:
        result = tools.apply_item(db, _hr(db), StubLLM(), _token(db), item)
        assert result.ok, result.summary
        assert db.get(Candidate, cid).status == "rejected"
        assert db.query(AgentRun).filter_by(agent="recruiter").one().delegation["actors"] == [
            "agent:recruiter", "agent:copilot"]


def test_bad_tool_calls_fail_politely(client, hr, job):
    assert "no tool" in _run("delete_everything", {}).summary.lower()
    missing = _run("get_candidate", {})
    assert not missing.ok and "candidate_id" in missing.summary
    wrong = _run("get_candidate", {"candidate_id": "abc"})
    assert not wrong.ok
    unknown = _run("get_candidate", {"candidate_id": 9999})
    assert not unknown.ok and "not found" in unknown.summary.lower()
    bad_stage = _run("propose_stage_change", {"candidate_id": 1, "stage": "fired"})
    assert not bad_stage.ok
