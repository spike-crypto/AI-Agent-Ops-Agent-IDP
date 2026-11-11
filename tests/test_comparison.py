from tests.test_agents import add


def test_compare_orders_by_score_and_explains(client, hr, job):
    a = add(client, hr, job["id"], "Strong One", "Python FastAPI Docker PostgreSQL")
    b = add(client, hr, job["id"], "Weak One", "Photoshop")
    client.post("/agents/screening/screen", headers=hr, json={"job_id": job["id"]})

    r = client.post("/agents/comparison/compare", headers=hr, json={"candidate_ids": [b, a]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["name"] for c in body["candidates"]] == ["Strong One", "Weak One"]
    assert "python" in body["candidates"][0]["strengths"]
    assert body["overview"]


def test_compare_is_read_only(client, hr, job):
    a = add(client, hr, job["id"], "A One", "Python")
    b = add(client, hr, job["id"], "B Two", "Docker")
    client.post("/agents/comparison/compare", headers=hr, json={"candidate_ids": [a, b]})
    run = client.get("/agent-runs", headers=hr).json()[0]
    assert run["agent"] == "comparison" and run["status"] == "success"
    assert set(run["scopes_used"]) <= {"job:read", "candidate:read"}
    assert client.get("/drafts", headers=hr).json() == []  # wrote nothing


def test_compare_validates_input(client, hr, job):
    a = add(client, hr, job["id"], "Only One", "Python")
    assert client.post("/agents/comparison/compare", headers=hr, json={"candidate_ids": [a]}).status_code == 400
    assert client.post("/agents/comparison/compare", headers=hr, json={"candidate_ids": [a, a]}).status_code == 400

    other = client.post("/jobs", headers=hr, json={"title": "Designer", "requirements": "Figma"}).json()
    c = add(client, hr, other["id"], "Other Job", "Figma")
    r = client.post("/agents/comparison/compare", headers=hr, json={"candidate_ids": [a, c]})
    assert r.status_code == 400 and "same job" in r.json()["detail"]


def test_interviewer_cannot_compare(client, hr, interviewer, job):
    a = add(client, hr, job["id"], "A One", "Python")
    b = add(client, hr, job["id"], "B Two", "Docker")
    r = client.post("/agents/comparison/compare", headers=interviewer["headers"], json={"candidate_ids": [a, b]})
    assert r.status_code == 403


def test_dashboard_counts_what_needs_you(client, hr, interviewer, job):
    cid = add(client, hr, job["id"], "Ravi Kumar", "Python")
    add(client, hr, job["id"], "Second Person", "Docker")
    d = client.get("/dashboard", headers=hr).json()
    assert d["needs_you"]["resumes_to_screen"] == 2
    assert d["setup"] == {"has_job": True, "has_candidates": True, "has_screened": False,
                          "has_interviewer": True, "has_interview": False}

    client.post("/agents/writing/job_description", headers=hr, json={"job_id": job["id"]})
    client.post("/agents/scheduling/propose", headers=hr, json={
        "candidate_id": cid, "interviewer_id": interviewer["id"], "slots": ["2020-01-01T10:00"]})
    d = client.get("/dashboard", headers=hr).json()
    assert d["needs_you"]["drafts_to_review"] == 2      # job description + invite
    assert d["needs_you"]["interviews_to_confirm"] == 1
    assert d["needs_you"]["resumes_to_screen"] == 1     # Ravi moved to interviewing
    assert d["jobs"][0]["stages"]["interviewing"] == 1


def test_dashboard_is_hr_only(client, interviewer):
    assert client.get("/dashboard", headers=interviewer["headers"]).status_code == 403


def test_icons_are_served(client):
    r = client.get("/static/vendor/lucide.min.js")
    assert r.status_code == 200 and "lucide" in r.text[:200]


def test_ui_is_revalidated_after_deploys(client):
    for path in ("/", "/static/app.js"):
        assert client.get(path).headers["cache-control"] == "no-cache"
    assert "cache-control" not in client.get("/health").headers
