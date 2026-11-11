import os

# Must be set before the app is imported: in-memory DB, offline LLM, known admin.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["LLM_PROVIDER"] = "fake"
os.environ["JWT_SECRET"] = "test-secret-that-is-at-least-32-bytes-long"
os.environ["ADMIN_EMAIL"] = "hr@example.com"
os.environ["ADMIN_PASSWORD"] = "hr-password-123"
os.environ.pop("AGENTOPS_API_KEY", None)
os.environ.pop("OPENROUTER_API_KEY", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import Base, engine  # noqa: E402
from app.main import app, init_db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    init_db()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def login(client, email="hr@example.com", password="hr-password-123") -> dict:
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def hr(client):
    return login(client)


@pytest.fixture
def interviewer(client, hr):
    r = client.post("/users", headers=hr, json={
        "email": "ivan@example.com", "name": "Ivan Interviewer", "password": "ivan-password-1",
        "role": "interviewer"})
    assert r.status_code == 201, r.text
    return {"id": r.json()["id"], "headers": login(client, "ivan@example.com", "ivan-password-1")}


@pytest.fixture
def job(client, hr):
    r = client.post("/jobs", headers=hr, json={
        "title": "Backend Engineer", "department": "Engineering",
        "requirements": "Python FastAPI Docker PostgreSQL", "salary_band": "INR 12-16 LPA"})
    assert r.status_code == 201
    return r.json()
