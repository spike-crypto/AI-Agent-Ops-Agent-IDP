from tests.conftest import login


def test_login_wrong_password(client):
    r = client.post("/auth/login", json={"email": "hr@example.com", "password": "nope"})
    assert r.status_code == 401


def test_protected_routes_need_a_token(client):
    assert client.get("/jobs").status_code == 401
    assert client.get("/jobs", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_me(client, hr):
    r = client.get("/auth/me", headers=hr)
    assert r.json()["role"] == "hr_admin"


def test_interviewer_cannot_use_hr_routes(client, interviewer):
    h = interviewer["headers"]
    assert client.get("/jobs", headers=h).status_code == 403
    assert client.get("/drafts", headers=h).status_code == 403
    assert client.post("/users", headers=h, json={
        "email": "x@example.com", "name": "X", "password": "password123"}).status_code == 403


def test_deactivated_user_is_locked_out(client, hr, interviewer):
    assert client.post(f"/users/{interviewer['id']}/deactivate", headers=hr).status_code == 200
    assert client.get("/auth/me", headers=interviewer["headers"]).status_code == 401
    r = client.post("/auth/login", json={"email": "ivan@example.com", "password": "ivan-password-1"})
    assert r.status_code == 401


def test_login_is_case_insensitive_on_email(client):
    assert login(client, email="HR@Example.com")
