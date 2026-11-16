import json

from app.db import SessionLocal
from app.models import AgentRun

READ_TOOLS = {"list_jobs", "find_job", "list_candidates", "get_candidate",
              "list_interviews", "pending_drafts"}


def rpc(client, headers, method, params=None, request_id=1):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", headers=headers, json=body)


def test_requires_sign_in(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 401


def test_interviewer_is_refused(client, interviewer):
    r = rpc(client, interviewer["headers"], "initialize")
    assert r.status_code == 403


def test_initialize(client, hr):
    r = rpc(client, hr, "initialize")
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["protocolVersion"]
    assert result["serverInfo"]["name"] == "hiring-desk"
    assert "tools" in result["capabilities"]


def test_notification_is_accepted_without_a_body(client, hr):
    r = client.post("/mcp", headers=hr, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202
    assert r.content == b""


def test_tools_list_exposes_only_the_read_tools(client, hr):
    r = rpc(client, hr, "tools/list")
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(tools) == READ_TOOLS
    assert tools["list_jobs"]["inputSchema"] == {"type": "object", "properties": {}}
    assert tools["list_candidates"]["inputSchema"]["required"] == ["job_id"]
    assert tools["list_candidates"]["inputSchema"]["properties"]["top"] == {"type": "integer"}
    assert tools["find_job"]["annotations"]["readOnlyHint"] is True


def test_tools_call_lists_jobs(client, hr):
    r = rpc(client, hr, "tools/call", {"name": "list_jobs", "arguments": {}})
    result = r.json()["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["summary"] == "Found 0 jobs"
    assert payload["data"]["jobs"] == []


def test_tools_call_reads_a_jobs_candidates(client, hr, job):
    created = client.post(f"/jobs/{job['id']}/candidates", headers=hr, json={
        "name": "Arun Kumar", "email": "arun@mail.test",
        "resume_text": "Python FastAPI Docker PostgreSQL for four years.",
    })
    assert created.status_code == 201, created.text

    r = rpc(client, hr, "tools/call", {"name": "list_candidates", "arguments": {"job_id": job["id"]}})
    result = r.json()["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["data"]["candidates"][0]["name"] == "Arun Kumar"


def test_bad_arguments_come_back_as_a_tool_error(client, hr):
    r = rpc(client, hr, "tools/call", {"name": "find_job", "arguments": {}})
    result = r.json()["result"]
    assert result["isError"] is True
    assert "needs 'name'" in result["content"][0]["text"]


def test_write_and_delegate_tools_are_not_exposed(client, hr):
    for name in ("screen", "compare", "propose_stage_change", "decision_memo"):
        r = rpc(client, hr, "tools/call", {"name": name, "arguments": {}})
        assert r.json()["error"]["code"] == -32602, name


def test_unknown_method(client, hr):
    r = rpc(client, hr, "resources/list")
    assert r.json()["error"]["code"] == -32601


def test_batch_requests_are_rejected(client, hr):
    r = client.post("/mcp", headers=hr, json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32600


def test_malformed_json_is_a_parse_error(client, hr):
    r = client.post("/mcp", headers={**hr, "Content-Type": "application/json"}, content=b"{oops")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32700


def test_each_call_is_audited(client, hr):
    rpc(client, hr, "tools/call", {"name": "list_jobs", "arguments": {}})
    with SessionLocal() as db:
        run = db.query(AgentRun).filter_by(agent="mcp", action="list_jobs").one()
    assert run.status == "success"
    assert run.scopes_used == ["job:read"]
    assert "Found 0 jobs" in run.summary
