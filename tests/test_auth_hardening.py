import jwt
from fastapi.testclient import TestClient

from app import ratelimit, totp
from app.db import SessionLocal
from app.main import app
from app.models import RefreshToken, User
from app.security import decode_access_token
from tests.conftest import login, refresh_cookie


def test_refresh_cookie_is_http_only_and_rotates(client, hr):
    raw = refresh_cookie(client)
    r = client.post("/auth/refresh")
    assert r.status_code == 200, r.text
    assert decode_access_token(r.json()["access_token"])["sub"] == "1"
    assert client.cookies.get("refresh_token") != raw
    # The presented token is now revoked, the replacement is live.
    with SessionLocal() as db:
        rows = db.query(RefreshToken).all()
        assert sum(1 for t in rows if t.revoked_at is None) == 1
        assert any(t.replaced_by for t in rows)


def test_replaying_a_rotated_refresh_token_kills_every_session(client, hr):
    old = refresh_cookie(client)
    assert client.post("/auth/refresh").status_code == 200
    current = client.cookies.get("refresh_token")

    r = client.post(f"/auth/refresh?refresh_token={old}")
    assert r.status_code == 401

    with SessionLocal() as db:
        assert all(t.revoked_at is not None for t in db.query(RefreshToken).all())
    client.cookies.set("refresh_token", current, path="/auth")
    assert client.post("/auth/refresh").status_code == 401


def test_refresh_without_a_token_is_rejected(client):
    assert client.post("/auth/refresh").status_code == 401
    assert client.post("/auth/refresh?refresh_token=nonsense").status_code == 401


def test_logout_revokes_this_device_but_not_other_devices(client, hr):
    other = TestClient(app)
    login(other)
    other_cookie = refresh_cookie(other)

    r = client.post("/auth/logout")
    assert r.status_code == 204
    assert client.cookies.get("refresh_token") is None
    assert client.post("/auth/refresh").status_code == 401
    assert client.get("/auth/me", headers=hr).status_code == 200  # access token lives on

    other.cookies.set("refresh_token", other_cookie, path="/auth")
    assert other.post("/auth/refresh").status_code == 200


def test_logout_can_also_kill_every_device(client, hr):
    other = TestClient(app)
    other_headers = login(other)
    r = client.post("/auth/logout", json={"all_devices": True})
    assert r.status_code == 204
    assert client.get("/auth/me", headers=hr).status_code == 401
    assert other.post("/auth/refresh").status_code == 401
    assert other.get("/auth/me", headers=other_headers).status_code == 401


def test_logout_all_kills_access_tokens_and_every_refresh_token(client, hr):
    other = TestClient(app)
    other_headers = login(other)

    assert client.post("/auth/logout-all", headers=hr).status_code == 204
    assert client.get("/auth/me", headers=hr).status_code == 401       # token_version bumped
    assert client.post("/auth/refresh").status_code == 401
    assert other.get("/auth/me", headers=other_headers).status_code == 401
    assert other.post("/auth/refresh").status_code == 401


def test_change_password_requires_current_and_revokes_sessions(client, hr):
    assert client.post("/auth/change-password", headers=hr, json={
        "current_password": "wrong-one", "new_password": "brand-new-password"}).status_code == 400

    r = client.post("/auth/change-password", headers=hr, json={
        "current_password": "hr-password-123", "new_password": "brand-new-password"})
    assert r.status_code == 200, r.text
    new_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert client.get("/auth/me", headers=new_headers).status_code == 200
    assert client.get("/auth/me", headers=hr).status_code == 401
    assert client.post("/auth/login", json={
        "email": "hr@example.com", "password": "hr-password-123"}).status_code == 401
    assert client.post("/auth/login", json={
        "email": "hr@example.com", "password": "brand-new-password"}).status_code == 200


def test_password_reset_flow(client, hr):
    r = client.post("/auth/forgot-password", json={"email": "hr@example.com"})
    assert r.status_code == 202
    token = r.json()["reset_token"]            # test env only; production never returns it

    bad = client.post("/auth/reset-password", json={"token": token, "new_password": "short"})
    assert bad.status_code == 422

    r = client.post("/auth/reset-password", json={"token": token, "new_password": "reset-password-1"})
    assert r.status_code == 200, r.text
    assert client.post("/auth/refresh").status_code == 401     # old sessions are gone
    assert client.post("/auth/login", json={
        "email": "hr@example.com", "password": "reset-password-1"}).status_code == 200
    # One-time link.
    assert client.post("/auth/reset-password", json={
        "token": token, "new_password": "another-password-1"}).status_code == 400
    assert client.post("/auth/reset-password", json={
        "token": "made-up", "new_password": "another-password-1"}).status_code == 400


def test_forgot_password_hides_whether_an_email_exists(client):
    r = client.post("/auth/forgot-password", json={"email": "nobody@example.com"})
    assert r.status_code == 202
    assert "reset_token" not in r.json()
    assert "registered" in r.json()["message"]


def test_forgot_password_is_rate_limited_per_email(client):
    for _ in range(3):
        client.post("/auth/forgot-password", json={"email": "hr@example.com"})
    r = client.post("/auth/forgot-password", json={"email": "hr@example.com"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_hr_can_issue_a_reset_link_and_it_works(client, hr, interviewer):
    r = client.post(f"/users/{interviewer['id']}/password-reset", headers=interviewer["headers"])
    assert r.status_code == 403
    r = client.post(f"/users/{interviewer['id']}/password-reset", headers=hr)
    assert r.status_code == 201, r.text
    token = r.json()["reset_token"]
    assert r.json()["reset_url"].endswith(f"?reset={token}")
    assert client.post("/auth/reset-password", json={
        "token": token, "new_password": "handed-over-1"}).status_code == 200
    assert login(client, "ivan@example.com", "handed-over-1")


def test_lockout_after_repeated_wrong_passwords(client):
    for _ in range(5):
        r = client.post("/auth/login", json={"email": "hr@example.com", "password": "nope"})
        assert r.status_code == 401
    # Even the right password is refused while the account is locked...
    ratelimit.reset("login:email:hr@example.com")
    r = client.post("/auth/login", json={"email": "hr@example.com", "password": "hr-password-123"})
    assert r.status_code == 429 and "Retry-After" in r.headers
    # ...and the limiter blocks before the password is even checked.
    with SessionLocal() as db:
        user = db.query(User).filter_by(email="hr@example.com").one()
        assert user.locked_until is not None


def test_unknown_email_and_bad_password_look_identical(client, interviewer):
    unknown = client.post("/auth/login", json={"email": "ghost@example.com", "password": "whatever1"})
    wrong = client.post("/auth/login", json={"email": "hr@example.com", "password": "whatever1"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_deactivated_user_loses_access_and_cannot_log_in(client, hr, interviewer):
    r = client.post(f"/users/{interviewer['id']}/deactivate", headers=hr)
    assert r.status_code == 200, r.text
    assert client.get("/auth/me", headers=interviewer["headers"]).status_code == 401
    assert client.post("/auth/login", json={
        "email": "ivan@example.com", "password": "ivan-password-1"}).status_code == 401


def test_last_active_admin_cannot_be_removed(client, hr):
    me = client.get("/auth/me", headers=hr).json()
    assert client.post(f"/users/{me['id']}/deactivate", headers=hr).status_code == 400

    second = client.post("/users", headers=hr, json={
        "email": "admin2@example.com", "name": "Admin Two",
        "password": "admin-two-1", "role": "hr_admin"}).json()
    assert client.post(f"/users/{second['id']}/deactivate", headers=hr).status_code == 200
    # One admin left: now neither admin can be deactivated.
    assert client.post(f"/users/{me['id']}/deactivate", headers=hr).status_code == 400


def test_mfa_challenge_and_recovery_code(client, hr):
    hr_id = client.get("/auth/me", headers=hr).json()["id"]
    setup = client.post("/auth/mfa/setup", headers=hr).json()
    assert setup["otpauth_url"].startswith("otpauth://totp/")
    assert client.post("/auth/mfa/enable", headers=hr, json={"code": "000000"}).status_code == 400
    enabled = client.post("/auth/mfa/enable", headers=hr, json={"code": totp.current_code(setup["secret"])})
    assert enabled.status_code == 200, enabled.text
    codes = enabled.json()["recovery_codes"]
    assert len(codes) == 8

    anon = TestClient(app)
    r = anon.post("/auth/login", json={"email": "hr@example.com", "password": "hr-password-123"})
    assert r.status_code == 200
    assert r.json()["mfa_required"] is True and "access_token" not in r.json()
    assert anon.cookies.get("refresh_token") is None
    mfa_token = r.json()["mfa_token"]

    # An MFA challenge token is not an access token.
    assert anon.get("/auth/me", headers={"Authorization": f"Bearer {mfa_token}"}).status_code == 401

    assert anon.post("/auth/mfa/verify", json={"mfa_token": mfa_token, "code": "123"}).status_code == 401
    ok = anon.post("/auth/mfa/verify", json={"mfa_token": mfa_token, "code": codes[0]})
    assert ok.status_code == 200, ok.text
    assert anon.get("/auth/me", headers={
        "Authorization": f"Bearer {ok.json()['access_token']}"}).json()["id"] == hr_id

    # A recovery code works once.
    again = anon.post("/auth/login", json={"email": "hr@example.com", "password": "hr-password-123"})
    reuse = anon.post("/auth/mfa/verify", json={
        "mfa_token": again.json()["mfa_token"], "code": codes[0]})
    assert reuse.status_code == 401


def test_mfa_disable_needs_password_and_a_live_code(client, hr):
    hr_id = client.get("/auth/me", headers=hr).json()["id"]
    setup = client.post("/auth/mfa/setup", headers=hr).json()
    client.post("/auth/mfa/enable", headers=hr, json={"code": totp.current_code(setup["secret"])})

    assert client.post("/auth/mfa/disable", headers=hr, json={
        "password": "wrong", "code": totp.current_code(setup["secret"])}).status_code == 400
    assert client.post("/auth/mfa/disable", headers=hr, json={
        "password": "hr-password-123", "code": "000000"}).status_code == 400
    r = client.post("/auth/mfa/disable", headers=hr, json={
        "password": "hr-password-123", "code": totp.current_code(setup["secret"])})
    assert r.status_code == 200, r.text
    assert client.get("/auth/me", headers=hr).status_code == 401   # old token_version
    fresh = client.post("/auth/login", json={"email": "hr@example.com", "password": "hr-password-123"})
    assert "access_token" in fresh.json() and fresh.json()["user"]["id"] == hr_id


def test_access_tokens_carry_a_version_and_expiry(client, hr):
    token = hr["Authorization"].split()[1]
    payload = decode_access_token(token)
    assert payload["ver"] == 0
    assert payload["exp"] > payload["iat"]
    forged = jwt.encode({**payload, "ver": 99}, "test-secret-that-is-at-least-32-bytes-long",
                        algorithm="HS256")
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_sso_is_off_when_not_configured(client):
    config = client.get("/auth/config").json()
    assert config == {"mfa_supported": True, "oidc_enabled": False,
                      "oidc_button_label": "Sign in with SSO", "password_min_length": 8}
    assert client.get("/auth/oidc/login", follow_redirects=False).status_code == 404
