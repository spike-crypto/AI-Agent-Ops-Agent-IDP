"""Agent identity and delegated tokens (the OAuth 2.0 token-exchange pattern, RFC 8693 style).

Every agent has an identity, `agent:<name>`. An agent never runs on behalf of a person with
blanket access. Instead it presents a short-lived token that says:

    sub    the person it acts for          ("user:3")
    act    who is acting, outermost first  (agent:screening, acting for agent:copilot)
    aud    the one agent it is valid for   ("agent:screening")
    scope  only the permissions that step needs
    exp    5 minutes after issue
    jti    a single-use id; a second use is refused

`issue_for_user` turns a signed-in person into a token for one agent (exchange 1).
`exchange` lets an agent holding a token obtain a narrower token for another agent (exchange 2).
`verify` checks a token before an agent runs, and by default marks it used.

Honest limit: the agents share one process, so this boundary is enforced by code rather than by
the network. What it buys today is least privilege per step, replay protection and a complete
audit trail (`token_grants`) of who acted for whom. The token itself is never stored or logged.
"""

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.agents.base import ROLE_SCOPES, AgentPermissionError
from app.config import settings
from app.logging_config import get_logger
from app.models import TokenGrant, User

log = get_logger("hr_agents.identity")

TOKEN_SECONDS = 300
TOKEN_TYPE = "agent+jwt"
ISSUER = "hiring-desk"


class TokenError(AgentPermissionError):
    """A delegated token was missing, invalid, expired, replayed or for the wrong agent."""


@dataclass(frozen=True)
class Grant:
    jti: str
    user_id: int
    audience: str
    actors: tuple[str, ...]
    scopes: frozenset[str]
    context: dict = field(default_factory=dict)


def agent_id(name: str) -> str:
    return f"agent:{name}"


def _key() -> bytes:
    # Derived from JWT_SECRET but distinct from it, so a user access token can never
    # pass as an agent token (and the other way round).
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b"agent-delegation").derive(settings.jwt_secret.encode())


def _agent_scopes(name: str) -> frozenset[str]:
    """Everything a token for this agent may carry: what it uses plus what it may hand on."""
    from app.agents.registry import AGENTS

    agent = AGENTS.get(name)
    if agent is None:
        raise TokenError(f"No agent named '{name}'")
    return agent.scopes | agent.delegable_scopes


def _naive(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def _nest(actors: tuple[str, ...]) -> dict:
    """("agent:a", "agent:b") -> {"sub": "agent:a", "act": {"sub": "agent:b"}}"""
    claim: dict = {}
    for actor in reversed(actors):
        claim = {"sub": actor, **({"act": claim} if claim else {})}
    return claim


def _flatten(act: dict) -> tuple[str, ...]:
    out = []
    while isinstance(act, dict) and act.get("sub"):
        out.append(str(act["sub"]))
        act = act.get("act") or {}
    return tuple(out)


def _mint(db: Session, user: User, audience: str, actors: tuple[str, ...],
          scopes: frozenset[str], context: dict) -> str:
    now = time.time()
    jti = uuid.uuid4().hex
    claims = {
        "iss": ISSUER, "sub": f"user:{user.id}", "role": user.role, "aud": audience,
        "act": _nest(actors), "scope": sorted(scopes), "ctx": context or {},
        "jti": jti, "iat": int(now), "exp": int(now) + TOKEN_SECONDS,
    }
    token = jwt.encode(claims, _key(), algorithm="HS256", headers={"typ": TOKEN_TYPE})
    db.add(TokenGrant(jti=jti, user_id=user.id, audience=audience, actors=list(actors),
                      scopes=sorted(scopes), context=context or {}, issued_at=_naive(now),
                      expires_at=_naive(now + TOKEN_SECONDS)))
    db.commit()
    log.info("token.issued", extra={"jti": jti, "aud": audience, "actors": list(actors),
                                    "scopes": sorted(scopes), "user": user.email})
    return token


def issue_for_user(db: Session, user: User, agent: str, scopes: Iterable[str], context: dict) -> str:
    """Exchange 1: a signed-in person becomes a token for one agent.
    Scopes = requested ∩ the person's role ∩ what that agent may ever do."""
    granted = frozenset(scopes) & ROLE_SCOPES.get(user.role, frozenset()) & _agent_scopes(agent)
    return _mint(db, user, agent_id(agent), (agent_id(agent),), granted, context)


def exchange(db: Session, token: str, target: str, scopes: Iterable[str]) -> str:
    """Exchange 2: the agent holding `token` asks for a narrower token for `target`.
    Scopes = requested ∩ what `target` may ever do ∩ what the presented token already has."""
    presented = _decode(db, token, audience=None)
    user = db.get(User, presented.user_id)
    granted = frozenset(scopes) & _agent_scopes(target) & presented.scopes
    return _mint(db, user, agent_id(target), (agent_id(target),) + presented.actors,
                 granted, presented.context)


def verify(db: Session, token: str, audience: str, consume: bool = True) -> Grant:
    """Check a token for `audience`. With `consume`, mark it used so it cannot run twice."""
    grant = _decode(db, token, audience=audience)
    if consume:
        claimed = db.execute(
            update(TokenGrant).where(TokenGrant.jti == grant.jti, TokenGrant.used_at.is_(None))
            .values(used_at=_naive(time.time()))
        )
        db.commit()
        if claimed.rowcount != 1:
            raise TokenError(f"Delegated token {grant.jti[:8]} was already used")
    return grant


def _decode(db: Session, token: str, audience: str | None) -> Grant:
    try:
        if jwt.get_unverified_header(token).get("typ") != TOKEN_TYPE:
            raise TokenError("Not a delegated agent token")
        claims = jwt.decode(
            token, _key(), algorithms=["HS256"], issuer=ISSUER,
            audience=audience, options={"require": ["exp", "jti", "sub", "aud", "act"],
                                        "verify_aud": audience is not None, "verify_exp": False},
        )
    except jwt.InvalidAudienceError:
        raise TokenError(f"Delegated token is not valid for {audience}") from None
    except jwt.PyJWTError as exc:
        raise TokenError(f"Delegated token is invalid: {exc}") from None
    # Expiry is checked here, against time.time(), so the lifetime has one source of truth.
    if time.time() >= int(claims["exp"]):
        raise TokenError("Delegated token has expired")

    row = db.get(TokenGrant, claims["jti"])
    if row is None:
        raise TokenError("Delegated token was not issued here")
    user_id = int(str(claims["sub"]).removeprefix("user:"))
    user = db.get(User, user_id)
    if user is None or not user.is_active or user.role != claims.get("role"):
        raise TokenError("The person this token acts for is no longer allowed to")
    return Grant(jti=claims["jti"], user_id=user_id, audience=claims["aud"],
                 actors=_flatten(claims["act"]), scopes=frozenset(claims.get("scope") or []),
                 context=dict(claims.get("ctx") or {}))
