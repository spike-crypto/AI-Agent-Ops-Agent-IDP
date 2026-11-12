import os

# Must be set before the app is imported: in-memory DB, deterministic test model, known admin.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["APP_ENV"] = "test"
os.environ["JWT_SECRET"] = "test-secret-that-is-at-least-32-bytes-long"
os.environ["ADMIN_EMAIL"] = "hr@example.com"
os.environ["ADMIN_PASSWORD"] = "hr-password-123"
os.environ["RUN_WORKER"] = "false"
os.environ["COMPANY_NAME"] = "Acme Test"
os.environ["LOG_LEVEL"] = "WARNING"
# Empty (not popped): load_dotenv() must not refill these from a developer's .env.
os.environ["AGENTOPS_API_KEY"] = ""
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["OIDC_ISSUER"] = ""
os.environ["OIDC_CLIENT_ID"] = ""
os.environ["OIDC_CLIENT_SECRET"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import ratelimit  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.llm import get_llm_optional  # noqa: E402
from app.main import app, init_db  # noqa: E402
from tests.stub_llm import StubLLM  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    init_db()
    ratelimit.clear_all()
    app.dependency_overrides[get_llm_optional] = lambda: StubLLM()
    yield
    app.dependency_overrides.clear()
    ratelimit.clear_all()


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
        "title": "Backend Engineer", "department": "Engineering", "location": "Remote",
        "requirements": "Python FastAPI Docker PostgreSQL", "salary_band": "INR 12-16 LPA"})
    assert r.status_code == 201
    return r.json()


@pytest.fixture
def public_job(client, hr, job):
    r = client.patch(f"/jobs/{job['id']}", headers=hr, json={"published": True})
    assert r.status_code == 200, r.text
    return r.json()


def refresh_cookie(client) -> str:
    value = client.cookies.get("refresh_token")
    assert value, "login should set an httpOnly refresh cookie"
    return value


def override_llm(stub) -> None:
    app.dependency_overrides[get_llm_optional] = lambda: stub
