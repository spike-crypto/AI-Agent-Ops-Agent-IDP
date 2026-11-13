"""Delegated agent tokens (token exchange): least privilege per step, single use, audited."""

import logging
import time

import pytest

from app import identity
from app.agents.base import AgentPermissionError
from app.agents.runner import run_agent
from app.db import SessionLocal
from app.models import AgentRun, TokenGrant, User
from app.security import create_access_token
from tests.stub_llm import StubLLM
from tests.test_agents import add


def _user(db, email="hr@example.com") -> User:
    return db.query(User).filter_by(email=email).one()


def test_user_token_carries_only_shared_scopes(interviewer):
    with SessionLocal() as db:
        ivan = _user(db, "ivan@example.com")
        token = identity.issue_for_user(db, ivan, "screening", {"candidate:read", "candidate:write"}, {})
        grant = identity.verify(db, token, identity.agent_id("screening"))
    assert grant.scopes == frozenset({"candidate:read"})      # interviewers cannot write
    assert grant.actors == ("agent:screening",)


def test_exchange_narrows_scopes_and_nests_actors():
    with SessionLocal() as db:
        hr = _user(db)
        copilot = identity.issue_for_user(
            db, hr, "comparison", {"job:read", "candidate:read", "draft:write"}, {"turn": 7})
        step = identity.exchange(db, copilot, "screening", {"job:read", "candidate:read", "job:salary:read"})
        grant = identity.verify(db, step, identity.agent_id("screening"))
    assert grant.actors == ("agent:screening", "agent:comparison")
    assert grant.scopes == frozenset({"job:read", "candidate:read"})   # no salary: neither agent may
    assert grant.context == {"turn": 7}
    assert grant.user_id == hr.id


def test_wrong_audience_is_refused():
    with SessionLocal() as db:
        token = identity.issue_for_user(db, _user(db), "screening", {"job:read"}, {})
        with pytest.raises(identity.TokenError):
            identity.verify(db, token, identity.agent_id("writing"))


def test_replayed_token_is_refused():
    with SessionLocal() as db:
        token = identity.issue_for_user(db, _user(db), "screening", {"job:read"}, {})
        identity.verify(db, token, identity.agent_id("screening"))
        with pytest.raises(identity.TokenError, match="already used"):
            identity.verify(db, token, identity.agent_id("screening"))


def test_expired_token_is_refused(monkeypatch):
    with SessionLocal() as db:
        token = identity.issue_for_user(db, _user(db), "screening", {"job:read"}, {})
        real = time.time
        monkeypatch.setattr(identity.time, "time", lambda: real() + identity.TOKEN_SECONDS + 1)
        with pytest.raises(identity.TokenError, match="expired"):
            identity.verify(db, token, identity.agent_id("screening"))


def test_deactivated_user_invalidates_token(client, hr, interviewer):
    with SessionLocal() as db:
        token = identity.issue_for_user(db, _user(db, "ivan@example.com"), "screening", {"job:read"}, {})
    client.post(f"/users/{interviewer['id']}/deactivate", headers=hr)
    with SessionLocal() as db, pytest.raises(identity.TokenError):
        identity.verify(db, token, identity.agent_id("screening"))


def test_tampered_token_is_refused():
    with SessionLocal() as db:
        token = identity.issue_for_user(db, _user(db), "screening", {"job:read"}, {})
        head, body, sig = token.split(".")
        forged = ".".join([head, body, sig[:-4] + ("AAAA" if sig[-4:] != "AAAA" else "BBBB")])
        with pytest.raises(identity.TokenError):
            identity.verify(db, forged, identity.agent_id("screening"))


def test_user_access_token_is_not_a_delegation_token():
    with SessionLocal() as db:
        hr = _user(db)
        with pytest.raises(identity.TokenError):
            identity.verify(db, create_access_token(hr.id, hr.role, hr.token_version),
                            identity.agent_id("screening"))


def test_every_token_is_recorded_without_the_token_itself():
    with SessionLocal() as db:
        token = identity.issue_for_user(db, _user(db), "screening", {"job:read"}, {"turn": 1})
        row = db.query(TokenGrant).one()
        assert row.audience == "agent:screening" and row.scopes == ["job:read"] and row.used_at is None
        assert token not in str({c.name: getattr(row, c.name) for c in TokenGrant.__table__.columns})


def test_run_agent_with_delegation_records_actor_chain(client, hr, job):
    add(client, hr, job["id"], "Asha Rao", "Python")
    with SessionLocal() as db:
        user = _user(db)
        parent = identity.issue_for_user(
            db, user, "feedback", {"job:read", "candidate:read", "candidate:write"}, {})
        step = identity.exchange(db, parent, "screening", {"job:read", "candidate:read", "candidate:write"})
        run_agent(db, user, StubLLM(), "screening", "screen", {"job_id": job["id"]}, delegation=step)
        ok = db.query(AgentRun).order_by(AgentRun.id.desc()).first()
        assert ok.status == "success"
        assert ok.delegation["actors"] == ["agent:screening", "agent:feedback"]
        assert ok.delegation["jti"] and "candidate:write" in ok.scopes_used

        with pytest.raises(identity.TokenError):           # same token again: replay
            run_agent(db, user, StubLLM(), "screening", "screen", {"job_id": job["id"]}, delegation=step)
        denied = db.query(AgentRun).order_by(AgentRun.id.desc()).first()
        assert denied.status == "denied" and "already used" in denied.error


def test_delegated_scopes_are_enforced_inside_the_agent(client, hr, job):
    """A token without candidate:write cannot save scores, even though HR could."""
    add(client, hr, job["id"], "Asha Rao", "Python")
    with SessionLocal() as db:
        user = _user(db)
        step = identity.issue_for_user(db, user, "screening", {"job:read", "candidate:read"}, {})
        with pytest.raises(AgentPermissionError, match="candidate:write"):
            run_agent(db, user, StubLLM(), "screening", "screen", {"job_id": job["id"]}, delegation=step)
        denied = db.query(AgentRun).order_by(AgentRun.id.desc()).first()
        assert denied.status == "denied" and denied.delegation["scopes"] == ["candidate:read", "job:read"]


def test_token_for_another_user_is_refused(interviewer):
    with SessionLocal() as db:
        token = identity.issue_for_user(db, _user(db), "screening", {"job:read"}, {})
        with pytest.raises(identity.TokenError, match="different user"):
            run_agent(db, _user(db, "ivan@example.com"), StubLLM(), "screening", "screen",
                      {"job_id": 1}, delegation=token)


def test_tokens_never_logged(client, hr, job, caplog):
    caplog.set_level(logging.INFO)
    with SessionLocal() as db:
        user = _user(db)
        token = identity.issue_for_user(db, user, "screening", {"job:read", "candidate:read"}, {})
        jti = identity.verify(db, token, identity.agent_id("screening"), consume=False).jti
        run_agent(db, user, StubLLM(), "screening", "screen", {"job_id": job["id"]}, delegation=token)
    logged = " ".join(str(r.__dict__) for r in caplog.records)
    assert jti in logged and token not in logged
