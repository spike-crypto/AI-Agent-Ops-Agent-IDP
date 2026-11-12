from app.config import settings


def _apply(client, job_id, name="Asha Rao", email="asha.rao@gmail.com",
           resume=b"Asha Rao\nasha.rao@gmail.com\nPython FastAPI\n", filename="resume.txt",
           note=""):
    return client.post(
        f"/public/jobs/{job_id}/apply",
        data={"name": name, "email": email, "note": note},
        files={"resume": (filename, resume, "text/plain")},
    )


def test_public_board_shows_only_published_open_roles(client, hr, job):
    assert client.get("/public/jobs").json() == []
    assert client.get(f"/public/jobs/{job['id']}").status_code == 404

    published = client.patch(f"/jobs/{job['id']}", headers=hr, json={"published": True}).json()
    board = client.get("/public/jobs").json()
    assert [j["id"] for j in board] == [job["id"]]
    assert board[0]["location"] == "Remote"
    assert set(board[0]) == {"id", "title", "department", "location", "description", "requirements"}
    assert "INR 12-16 LPA" not in client.get("/public/jobs").text      # salary is internal
    assert "salary" not in client.get(f"/public/jobs/{job['id']}").text

    closed = client.patch(f"/jobs/{job['id']}", headers=hr, json={"status": "closed"})
    assert closed.status_code == 200
    assert client.get("/public/jobs").json() == []
    assert client.get(f"/public/jobs/{job['id']}").status_code == 404
    assert _apply(client, job["id"]).status_code == 404                  # cannot apply to a closed role


def test_an_application_lands_in_the_pipeline(client, hr, public_job):
    r = _apply(client, public_job["id"], note="Available from March.")
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "received"
    cid = r.json()["id"]

    cands = client.get(f"/jobs/{public_job['id']}/candidates", headers=hr).json()
    [cand] = [c for c in cands if c["id"] == cid]
    assert cand["name"] == "Asha Rao"
    assert cand["email"] == "asha.rao@gmail.com"
    assert cand["source"] == "careers"
    assert cand["status"] == "new"
    assert cand["resume_text"].endswith("APPLICANT NOTE:\nAvailable from March.")


def test_duplicate_applications_are_rejected(client, hr, public_job):
    assert _apply(client, public_job["id"]).status_code == 201
    same_person = _apply(client, public_job["id"], resume=b"Different resume text entirely\n")
    assert same_person.status_code == 409 and "already applied" in same_person.json()["detail"]
    same_resume = _apply(client, public_job["id"], name="Copy Cat", email="copy.cat@gmail.com")
    assert same_resume.status_code == 409

    other = client.post("/jobs", headers=hr, json={"title": "Data", "requirements": "SQL"}).json()
    client.patch(f"/jobs/{other['id']}", headers=hr, json={"published": True})
    assert _apply(client, other["id"]).status_code == 201                # a different role is fine


def test_bad_resumes_are_rejected_with_useful_messages(client, public_job):
    exe = _apply(client, public_job["id"], filename="cv.exe", resume=b"MZ...")
    assert exe.status_code == 400 and ".pdf" in exe.json()["detail"]
    blank = _apply(client, public_job["id"], resume=b"   \n\n  ")
    assert blank.status_code == 400
    broken_pdf = _apply(client, public_job["id"], filename="cv.pdf", resume=b"not really a pdf")
    assert broken_pdf.status_code == 400
    huge = _apply(client, public_job["id"], resume=b"a" * 2_000_001)
    assert huge.status_code == 413


def test_apply_is_rate_limited_per_connection(client, public_job):
    for i in range(settings.public_apply_per_hour):
        assert _apply(client, public_job["id"], name=f"Person {i}", email=f"person{i}@gmail.com",
                      resume=f"Resume number {i} with Python Docker\n").status_code == 201
    blocked = _apply(client, public_job["id"], name="One More", email="one.more@gmail.com",
                     resume="A unique resume for the last attempt\n")
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_public_config_and_careers_page(client):
    assert client.get("/public/config").json() == {
        "company_name": "Acme Test",
        "apply_rate_limit_per_hour": settings.public_apply_per_hour,
    }
    page = client.get("/careers")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "no-cache"
    assert "/public/jobs" in page.text and "Apply" in page.text


def test_the_public_api_never_requires_a_login(client, public_job):
    assert client.get("/public/jobs").status_code == 200
    assert client.get(f"/public/jobs/{public_job['id']}").status_code == 200
    assert client.get("/jobs").status_code == 401                       # internal API still guarded
    assert client.get(f"/jobs/{public_job['id']}/candidates").status_code == 401
