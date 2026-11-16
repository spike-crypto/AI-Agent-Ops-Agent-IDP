"""Integration keys, and the one path every outside read goes through.

An integration key is a long random secret an HR admin creates for an outside system such as
SAP Integration Suite. It is shown once; only its SHA-256 hash is stored. It works only on the
integration API (`/integration/v1/...`) and `/mcp`, never on the main API, and it can only read.

Each call exchanges the key for a single-use, 5-minute token for `agent:integration`
(`app/identity.py`), checks the tool's scopes against that token, runs one read tool and
records an AgentRun with the token's id. Revoking the key, or deactivating the HR admin who
created it, stops it at once.
"""

import base64
import binascii
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import identity, ratelimit
from app.agents.base import AgentPermissionError
from app.agents.integration import INTEGRATION_SCOPES
from app.copilot_tools import _READERS, TOOLS, ToolInputError, ToolResult, _coerce
from app.db import get_db
from app.logging_config import get_logger
from app.models import AgentRun, IntegrationKey, User, utcnow

log = get_logger("hr_agents.integration")

KEY_PREFIX = "hdk_"
REQUESTS_PER_MINUTE = 120
# Read tools whose scopes an integration key can hold (so not pending_drafts).
INTEGRATION_TOOLS = {n: t for n, t in TOOLS.items() if t.kind == "read" and t.scopes <= INTEGRATION_SCOPES}


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_key(db: Session, owner: User, name: str) -> tuple[IntegrationKey, str]:
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    row = IntegrationKey(name=name, prefix=key[:12], key_hash=_hash(key),
                         scopes=sorted(INTEGRATION_SCOPES), user_id=owner.id)
    db.add(row)
    db.commit()
    db.refresh(row)
    log.info("integration_key.created", extra={"key_id": row.id, "user": owner.email})
    return row, key


@dataclass
class Caller:
    key: IntegrationKey
    owner: User


def _presented_key(request: Request) -> str | None:
    """Bearer <key>, or Basic with the key as the password (SAP destinations default to Basic)."""
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer":
        return value.strip() or None
    if scheme.lower() == "basic":
        try:
            _, _, password = base64.b64decode(value.strip()).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None
        return password or None
    return None


def require_integration_key(request: Request, db: Session = Depends(get_db)) -> Caller:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "A valid integration key is required",
                                 headers={"WWW-Authenticate": 'Bearer, Basic realm="hiring-desk"'})
    presented = _presented_key(request)
    if not presented or not presented.startswith(KEY_PREFIX):
        raise unauthorized
    row = db.scalar(select(IntegrationKey).where(IntegrationKey.key_hash == _hash(presented)))
    if row is None or row.revoked_at is not None or not hmac.compare_digest(row.key_hash, _hash(presented)):
        raise unauthorized
    owner = db.get(User, row.user_id)
    if owner is None or not owner.is_active or owner.role != "hr_admin":
        raise unauthorized
    allowed, retry_after = ratelimit.hit(f"integration:{row.id}", REQUESTS_PER_MINUTE, 60)
    if not allowed:
        raise HTTPException(429, "Too many requests for this integration key",
                            headers={"Retry-After": str(retry_after)})
    return Caller(key=row, owner=owner)


def call(db: Session, caller: Caller, name: str, args: dict) -> ToolResult:
    """Run one read tool for an outside system, through a single-use agent token.

    Raises LookupError (not found), ToolInputError/ValueError (bad arguments) or
    AgentPermissionError (the token does not allow it); every outcome is recorded.
    """
    tool = INTEGRATION_TOOLS.get(name)
    if tool is None:
        raise AgentPermissionError(f"'{name}' is not available to integrations")
    started = time.perf_counter()
    grant, result, status_, error = None, None, "error", "failed unexpectedly"
    try:
        token = identity.issue_for_user(db, caller.owner, "integration", caller.key.scopes,
                                        {"integration_key": caller.key.id})
        grant = identity.verify(db, token, identity.agent_id("integration"))
        missing = sorted(tool.scopes - grant.scopes)
        if missing:
            raise AgentPermissionError(f"This integration key does not allow {', '.join(missing)}")
        result = _READERS[name](db, **_coerce(tool, args or {}))
        status_, error = "success", ""
        return result
    except (ToolInputError, LookupError, ValueError, AgentPermissionError) as exc:
        db.rollback()
        status_, error = ("denied" if isinstance(exc, AgentPermissionError) else "error"), str(exc)
        raise
    except Exception:
        db.rollback()
        raise
    finally:
        caller.key.last_used_at = utcnow()
        db.add(caller.key)
        db.add(AgentRun(
            agent="integration", action=name, user_id=caller.owner.id, status=status_,
            scopes_used=sorted(tool.scopes),
            summary=(result.summary[:500] if result else f"{name} by {caller.key.name}"),
            error=error[:2000], duration_ms=int((time.perf_counter() - started) * 1000),
            delegation=({"jti": grant.jti, "actors": list(grant.actors), "scopes": sorted(grant.scopes),
                         "integration_key": caller.key.id} if grant else {"integration_key": caller.key.id}),
        ))
        db.commit()
