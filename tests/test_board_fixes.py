from app.resume_parsing import find_email, guess_name
from tests.test_agents import add


def test_name_from_top_of_resume():
    assert guess_name("x.pdf", "Ananya Iyer - ananya@example.com\nBackend engineer") == "Ananya Iyer"
    assert guess_name("x.pdf", "RAVI KUMAR\nPython developer") == "Ravi Kumar"


def test_name_from_job_board_file_name():
    # The real-world case from the board: Naukri file names with an experience tag.
    text = "Summary\nPython developer with 4 years"
    assert guess_name("Naukri_Parimichowdary[4Y_0M].pdf", text) == "Parimichowdary"
    assert guess_name("Naukri_AmarjeetSingh[10Y_2M].pdf", text) == "AmarjeetSingh"
    assert guess_name("resume-final (2).pdf", "") == "Unnamed candidate"


def test_headings_and_titles_are_not_names():
    assert guess_name("priya_sharma.txt", "Software Engineer\nPython") == "Priya Sharma"
    assert guess_name("priya_sharma.txt", "Curriculum Vitae\nPython") == "Priya Sharma"


def test_find_email():
    assert find_email("Contact: Ravi.K@Mail.co.in, +91 99") == "ravi.k@mail.co.in"
    assert find_email("no email here") == ""


def test_upload_skips_duplicates(client, hr, job):
    one = ("files", ("a.txt", b"Asha Rao\nasha@mail.test Python", "text/plain"))
    r = client.post(f"/jobs/{job['id']}/candidates/upload", headers=hr, files=[one, one])
    assert len(r.json()["created"]) == 1
    assert r.json()["skipped"][0]["reason"] == "already added to this job"

    # Same email in a different file is also a duplicate; uploading again later is too.
    other = ("files", ("b.txt", b"Asha Rao\nasha@mail.test Python Docker", "text/plain"))
    r = client.post(f"/jobs/{job['id']}/candidates/upload", headers=hr, files=[one, other])
    assert r.json()["created"] == [] and len(r.json()["skipped"]) == 2

    # The same person can still apply to a different job.
    other_job = client.post("/jobs", headers=hr, json={"title": "Data", "requirements": "SQL"}).json()
    r = client.post(f"/jobs/{other_job['id']}/candidates/upload", headers=hr, files=[one])
    assert len(r.json()["created"]) == 1


def test_screening_in_batches(client, hr, job):
    for i in range(5):
        add(client, hr, job["id"], f"Person {i}", f"Python resume {i}")
    seen = 0
    for expected_remaining in (3, 1, 0):
        r = client.post("/agents/screening/screen", headers=hr, json={"job_id": job["id"], "limit": 2}).json()
        seen += r["processed"]
        assert r["remaining"] == expected_remaining
    assert seen == 5
    assert all(c["status"] == "screened" for c in client.get(f"/jobs/{job['id']}/candidates", headers=hr).json())


def test_screening_can_skip_failed_ones(client, hr, job):
    a = add(client, hr, job["id"], "Skip Me", "Python")
    add(client, hr, job["id"], "Do Me", "Docker")
    r = client.post("/agents/screening/screen", headers=hr,
                    json={"job_id": job["id"], "limit": 5, "exclude_ids": [a]}).json()
    assert r["processed"] == 1 and r["remaining"] == 0


def _scheduled_interview(client, hr, interviewer, job):
    cid = add(client, hr, job["id"], "Kit Person", "Python FastAPI")
    r = client.post("/agents/scheduling/propose", headers=hr, json={
        "candidate_id": cid, "interviewer_id": interviewer["id"], "slots": ["2030-01-01T10:00"]})
    return r.json()["interview_id"]


def test_interview_kit_goes_to_the_interviewer_not_the_candidate(client, hr, interviewer, job):
    iid = _scheduled_interview(client, hr, interviewer, job)
    draft = client.post("/agents/interview_kit/prepare", headers=hr, json={"interview_id": iid}).json()["draft"]
    assert draft["recipient"] == "ivan@example.com"           # the interviewer
    assert draft["recipient"] != "kit@mail.test"              # never the candidate
    assert "interviewer only" in draft["subject"]


def test_interview_kit_prompt_has_safety_rules():
    from app.agents.interview_kit import SYSTEM
    assert "untrusted" in SYSTEM and "protected characteristic" in SYSTEM
