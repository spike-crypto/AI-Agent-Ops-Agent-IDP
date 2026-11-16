"""Integration keys and the read-only integration API that SAP Integration Suite imports."""

import base64

import pytest

from app.db import SessionLocal
from app.models import AgentRun, IntegrationKey, TokenGrant, User


@pytest.fixture
def key(client, hr):
    r = client.post("/integration-keys", headers=hr, json={"name": "SAP CPI"})
    assert r.status_code == 201, r.text
    return r.json()


def bearer(k: dict) -> dict:
    return {"Authorization": f"Bearer {k['key']}"}


# ---- keys -----------------------------------------------------------------------------------

def test_a_key_is_shown_once_and_stored_only_as_a_hash(client, hr, key):
    assert key["key"].startswith("hdk_") and key["name"] == "SAP CPI"
    assert key["scopes"] == ["candidate:read", "interview:read", "job:read"]
    listed = client.get("/integration-keys", headers=hr).json()
    assert listed[0]["id"] == key["id"] and "key" not in listed[0]
    assert listed[0]["prefix"] == key["key"][:12]
    with SessionLocal() as db:
        row = db.get(IntegrationKey, key["id"])
        assert key["key"] not in (row.key_hash, row.prefix) and len(row.key_hash) == 64


def test_only_hr_manages_keys(client, interviewer):
    assert client.post("/integration-keys", headers=interviewer["headers"], json={"name": "x"}).status_code == 403
    assert client.get("/integration-keys", headers=interviewer["headers"]).status_code == 403


def test_a_revoked_key_stops_working(client, hr, key):
    assert client.get("/integration/v1/jobs", headers=bearer(key)).status_code == 200
    assert client.post(f"/integration-keys/{key['id']}/revoke", headers=hr).status_code == 200
    assert client.get("/integration/v1/jobs", headers=bearer(key)).status_code == 401


def test_a_key_stops_when_its_owner_is_deactivated(client, hr, key):
    with SessionLocal() as db:
        db.query(User).filter_by(email="hr@example.com").update({"is_active": False})
        db.commit()
    assert client.get("/integration/v1/jobs", headers=bearer(key)).status_code == 401


def test_a_key_cannot_use_the_main_api(client, key):
    """The whole point: the credential stored in SAP can read, and nothing else."""
    assert client.get("/jobs", headers=bearer(key)).status_code == 401
    assert client.post("/jobs", headers=bearer(key), json={"title": "x", "requirements": "y"}).status_code == 401


def test_a_normal_sign_in_cannot_use_the_integration_api(client, hr):
    assert client.get("/integration/v1/jobs", headers=hr).status_code == 401


def test_basic_auth_with_the_key_as_password_works(client, key):
    """SAP destinations default to BasicAuthentication."""
    basic = base64.b64encode(f"integration:{key['key']}".encode()).decode()
    assert client.get("/integration/v1/jobs", headers={"Authorization": f"Basic {basic}"}).status_code == 200


def test_wrong_or_missing_key(client):
    assert client.get("/integration/v1/jobs").status_code == 401
    assert client.get("/integration/v1/jobs", headers={"Authorization": "Bearer hdk_nope"}).status_code == 401


# ---- read endpoints -------------------------------------------------------------------------

def test_jobs_candidates_and_one_candidate(client, hr, job, key):
    created = client.post(f"/jobs/{job['id']}/candidates", headers=hr, json={
        "name": "Arun Kumar", "resume_text": "Python FastAPI Docker PostgreSQL"}).json()
    jobs = client.get("/integration/v1/jobs", headers=bearer(key)).json()
    assert jobs["data"]["jobs"][0]["id"] == job["id"]
    found = client.get("/integration/v1/jobs/search", params={"name": "backend"}, headers=bearer(key)).json()
    assert found["data"]["jobs"][0]["id"] == job["id"]
    cands = client.get(f"/integration/v1/jobs/{job['id']}/candidates", params={"top": 5}, headers=bearer(key)).json()
    assert cands["data"]["candidates"][0]["name"] == "Arun Kumar"
    assert cands["data"]["not_screened"] == ["Arun Kumar"]
    one = client.get(f"/integration/v1/candidates/{created['id']}", headers=bearer(key)).json()
    assert one["data"]["candidate"]["name"] == "Arun Kumar"
    assert client.get("/integration/v1/interviews", headers=bearer(key)).status_code == 200


def test_missing_things_are_404(client, key):
    assert client.get("/integration/v1/jobs/999/candidates", headers=bearer(key)).status_code == 404
    assert client.get("/integration/v1/candidates/999", headers=bearer(key)).status_code == 404


def test_every_call_runs_through_a_single_use_agent_token(client, key):
    client.get("/integration/v1/jobs", headers=bearer(key))
    with SessionLocal() as db:
        run = db.query(AgentRun).filter_by(agent="integration", action="list_jobs").one()
        grant = db.get(TokenGrant, run.delegation["jti"])
        assert run.status == "success" and run.delegation["integration_key"] == key["id"]
        assert grant.audience == "agent:integration" and grant.used_at is not None
        assert sorted(grant.scopes) == ["candidate:read", "interview:read", "job:read"]
        assert db.get(IntegrationKey, key["id"]).last_used_at is not None


def test_openapi_lists_only_the_integration_endpoints(client):
    spec = client.get("/integration/v1/openapi.json", headers={"X-Forwarded-Proto": "https"}).json()
    assert spec["openapi"].startswith("3.")
    assert set(spec["paths"]) == {"/integration/v1/jobs", "/integration/v1/jobs/search",
                                  "/integration/v1/jobs/{job_id}/candidates",
                                  "/integration/v1/candidates/{candidate_id}", "/integration/v1/interviews"}
    assert all(op.get("operationId") and op.get("description")
               for path in spec["paths"].values() for op in path.values())
    assert spec["servers"][0]["url"].startswith("https://")
    assert {"bearerAuth", "basicAuth"} <= set(spec["components"]["securitySchemes"])


def test_stage_values_in_the_spec_match_the_app(client):
    from app.models import CANDIDATE_STATUSES
    spec = client.get("/integration/v1/openapi.json").json()
    params = spec["paths"]["/integration/v1/jobs/{job_id}/candidates"]["get"]["parameters"]
    stage = next(p for p in params if p["name"] == "stage")["schema"]
    enum = next(s["enum"] for s in stage.get("anyOf", [stage]) if "enum" in s)
    assert tuple(enum) == CANDIDATE_STATUSES
