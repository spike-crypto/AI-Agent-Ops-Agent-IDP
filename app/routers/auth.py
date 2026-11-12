from datetime import timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import oidc, ratelimit, totp
from app.config import settings
from app.db import get_db
from app.deps import get_current_user, require_hr
from app.logging_config import get_logger
from app.models import ROLES, PasswordReset, RefreshToken, User, utcnow
from app.security import (
    create_access_token, create_mfa_token, decode_mfa_token, hash_password,
    hash_token, new_opaque_token, verify_password,
)

router = APIRouter(tags=["auth"])
log = get_logger("hr_agents.auth")

REFRESH_COOKIE = "refresh_token"
OIDC_COOKIE = "oidc_state"
_DUMMY_HASH = hash_password("not-a-real-password")  # equalises timing for unknown emails


class LoginIn(BaseModel):
    email: str
    password: str


class MfaVerifyIn(BaseModel):
    mfa_token: str
    code: str


class MfaCodeIn(BaseModel):
    code: str


class MfaDisableIn(BaseModel):
    password: str
    code: str


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=200)


class PasswordResetIn(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=200)


class ForgotPasswordIn(BaseModel):
    email: str


class UserIn(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8)
    role: str = "interviewer"


class LogoutIn(BaseModel):
    all_devices: bool = False


def user_out(u: User) -> dict:
    return {
        "id": u.id, "email": u.email, "name": u.name, "role": u.role, "is_active": u.is_active,
        "mfa_enabled": u.mfa_enabled,
    }


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "-")


def _limit(key: str, limit: int, window_seconds: int) -> None:
    allowed, retry = ratelimit.hit(key, limit, window_seconds)
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts. Wait a few minutes and try again.",
            headers={"Retry-After": str(retry)},
        )


def _set_refresh_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        raw,
        max_age=settings.refresh_token_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/auth",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path="/auth")


def _empty(response: Response) -> Response:
    """Return the injected response so its Set-Cookie headers survive (204)."""
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


def _issue_tokens(response: Response, db: Session, user: User, request: Request) -> dict:
    raw = new_opaque_token()
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=hash_token(raw),
        expires_at=utcnow() + timedelta(days=settings.refresh_token_days),
        user_agent=(request.headers.get("user-agent") or "")[:255],
        ip=_client_ip(request)[:64],
    ))
    db.commit()
    _set_refresh_cookie(response, raw)
    return {
        "access_token": create_access_token(user.id, user.role, user.token_version),
        "token_type": "bearer",
        "expires_in": settings.jwt_expiry_minutes * 60,
        "user": user_out(user),
    }


def _revoke_all(db: Session, user_id: int) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    db.commit()


def _successful_login(db: Session, user: User) -> None:
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    db.commit()


def _failed_login(db: Session, user: User | None) -> None:
    if user is None:
        return
    user.failed_logins += 1
    if user.failed_logins >= settings.login_max_attempts:
        user.locked_until = utcnow() + timedelta(minutes=settings.lockout_minutes)
        user.failed_logins = 0
        log.warning("auth.account_locked", extra={"user": user.email,
                                                  "minutes": settings.lockout_minutes})
    db.commit()


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    email = body.email.lower().strip()
    _limit(f"login:ip:{_client_ip(request)}", 30, settings.login_window_minutes * 60)
    _limit(f"login:email:{email}", settings.login_max_attempts, settings.login_window_minutes * 60)

    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        verify_password(body.password, _DUMMY_HASH)  # same work as a real check
        log.info("auth.login_failed", extra={"email": email, "reason": "unknown"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password")
    if not user.is_active:
        log.info("auth.login_failed", extra={"email": email, "reason": "inactive"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password")
    if user.locked_until and user.locked_until > utcnow():
        log.warning("auth.login_failed", extra={"email": email, "reason": "locked"})
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed attempts. Try again later.",
            headers={"Retry-After": str(int((user.locked_until - utcnow()).total_seconds()) + 1)},
        )
    if not verify_password(body.password, user.password_hash):
        _failed_login(db, user)
        log.info("auth.login_failed", extra={"email": email, "reason": "password"})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password")

    ratelimit.reset(f"login:email:{email}")
    _successful_login(db, user)

    if user.mfa_enabled:
        log.info("auth.mfa_challenge", extra={"user": email})
        return {"mfa_required": True, "mfa_token": create_mfa_token(user.id)}

    log.info("auth.login_ok", extra={"user": email, "role": user.role})
    return _issue_tokens(response, db, user, request)


@router.post("/auth/mfa/verify")
def verify_mfa(body: MfaVerifyIn, request: Request, response: Response, db: Session = Depends(get_db)):
    _limit(f"mfa:ip:{_client_ip(request)}", 30, settings.login_window_minutes * 60)
    try:
        payload = decode_mfa_token(body.mfa_token)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That sign-in attempt expired. Start again.")
    user = db.get(User, int(payload["sub"]))
    if user is None or not user.is_active or not user.mfa_enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "MFA is not available for this account")

    if not totp.verify(user.mfa_secret or "", body.code):
        matches = [h for h in (user.mfa_recovery or []) if h == totp.hash_recovery_code(body.code)]
        if not matches:
            _failed_login(db, user)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That code is not valid")
        user.mfa_recovery = [h for h in (user.mfa_recovery or []) if h != matches[0]]
        db.commit()
        log.info("auth.mfa_recovery_used", extra={"user": user.email})

    _successful_login(db, user)
    log.info("auth.login_ok", extra={"user": user.email, "role": user.role, "mfa": True})
    return _issue_tokens(response, db, user, request)


@router.post("/auth/refresh")
def refresh(request: Request, response: Response, db: Session = Depends(get_db),
            refresh_token: str | None = None):
    raw = refresh_token or request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)))
    if row is None:
        _clear_refresh_cookie(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired. Sign in again.")
    if row.revoked_at is not None:
        # A revoked token being replayed means it leaked: kill every session for the user.
        log.warning("auth.refresh_reuse", extra={"user_id": row.user_id})
        _revoke_all(db, row.user_id)
        _clear_refresh_cookie(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired. Sign in again.")
    if row.expires_at <= utcnow():
        _clear_refresh_cookie(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired. Sign in again.")
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        _clear_refresh_cookie(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")

    replaced = new_opaque_token()
    row.revoked_at = utcnow()
    row.replaced_by = hash_token(replaced)
    db.add(RefreshToken(
        user_id=user.id,
        token_hash=hash_token(replaced),
        expires_at=utcnow() + timedelta(days=settings.refresh_token_days),
        user_agent=(request.headers.get("user-agent") or "")[:255],
        ip=_client_ip(request)[:64],
    ))
    db.commit()
    _set_refresh_cookie(response, replaced)
    return {
        "access_token": create_access_token(user.id, user.role, user.token_version),
        "token_type": "bearer",
        "expires_in": settings.jwt_expiry_minutes * 60,
        "user": user_out(user),
    }


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, body: LogoutIn | None = None,
           db: Session = Depends(get_db)):
    """Revokes this device's refresh token. all_devices=True also kills every access token."""
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)))
        if row and row.revoked_at is None:
            row.revoked_at = utcnow()
            db.commit()
            if body and body.all_devices:
                user = db.get(User, row.user_id)
                if user:
                    user.token_version += 1
                    _revoke_all(db, user.id)
                    db.commit()
    _clear_refresh_cookie(response)
    return _empty(response)


@router.post("/auth/logout-all", status_code=204)
def logout_all(response: Response, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    _revoke_all(db, user.id)
    user.token_version += 1
    db.commit()
    _clear_refresh_cookie(response)
    log.info("auth.logout_all", extra={"user": user.email})
    return _empty(response)


@router.post("/auth/change-password")
def change_password(body: PasswordChangeIn, request: Request, response: Response,
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is wrong")
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1
    db.commit()
    _revoke_all(db, user.id)
    log.info("auth.password_changed", extra={"user": user.email})
    return _issue_tokens(response, db, user, request)


@router.post("/auth/forgot-password", status_code=202)
def forgot_password(body: ForgotPasswordIn, request: Request, db: Session = Depends(get_db)):
    email = body.email.lower().strip()
    _limit(f"forgot:ip:{_client_ip(request)}", 10, 3600)
    _limit(f"forgot:email:{email}", 3, 3600)
    user = db.scalar(select(User).where(User.email == email, User.is_active.is_(True)))
    payload = {"message": "If that email is registered, a reset link was created."}
    if user is not None:
        raw = new_opaque_token()
        expires = utcnow() + timedelta(minutes=settings.password_reset_minutes)
        db.add(PasswordReset(user_id=user.id, token_hash=hash_token(raw), expires_at=expires))
        db.commit()
        base = settings.public_base_url or str(request.base_url).rstrip("/")
        reset_url = f"{base}/?reset={raw}"
        log.info("auth.password_reset_requested", extra={"user": email, "expires_at": expires.isoformat()})
        if not settings.is_production:
            # Dev/demo convenience only. Production needs an email provider (or the HR
            # endpoint below) to deliver this link.
            payload.update(reset_token=raw, reset_url=reset_url)
    return payload


def _consume_reset(db: Session, raw: str) -> PasswordReset:
    row = db.scalar(select(PasswordReset).where(PasswordReset.token_hash == hash_token(raw)))
    if row is None or row.used_at is not None or row.expires_at <= utcnow():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That reset link is invalid or has expired")
    return row


@router.post("/auth/reset-password")
def reset_password(body: PasswordResetIn, db: Session = Depends(get_db)):
    row = _consume_reset(db, body.token)
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That reset link is invalid or has expired")
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1
    user.failed_logins = 0
    user.locked_until = None
    row.used_at = utcnow()
    db.commit()
    _revoke_all(db, user.id)
    log.info("auth.password_reset", extra={"user": user.email})
    return {"ok": True}


@router.get("/auth/config")
def auth_config():
    return {
        "mfa_supported": True,
        "oidc_enabled": settings.oidc_enabled,
        "oidc_button_label": settings.oidc_button_label,
        "password_min_length": 8,
    }


@router.post("/auth/mfa/setup")
def mfa_setup(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.mfa_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "MFA is already enabled")
    secret = totp.new_secret()
    user.mfa_secret = secret
    db.commit()
    return {
        "secret": secret,
        "otpauth_url": totp.provisioning_uri(secret, user.email, settings.mfa_issuer),
        "instructions": "Add this secret to any authenticator app, then confirm with a 6-digit code.",
    }


@router.post("/auth/mfa/enable")
def mfa_enable(body: MfaCodeIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.mfa_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "MFA is already enabled")
    if not user.mfa_secret or not totp.verify(user.mfa_secret, body.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That code is not valid. Check the clock on your phone.")
    codes = totp.new_recovery_codes()
    user.mfa_recovery = [totp.hash_recovery_code(c) for c in codes]
    user.mfa_enabled = True
    db.commit()
    log.info("auth.mfa_enabled", extra={"user": user.email})
    return {"ok": True, "recovery_codes": codes}


@router.post("/auth/mfa/disable")
def mfa_disable(body: MfaDisableIn, request: Request, response: Response,
                user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.mfa_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MFA is not enabled")
    if not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password is wrong")
    if not totp.verify(user.mfa_secret or "", body.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That code is not valid")
    user.mfa_enabled = False
    user.mfa_secret = None
    user.mfa_recovery = []
    user.token_version += 1
    db.commit()
    _revoke_all(db, user.id)
    log.info("auth.mfa_disabled", extra={"user": user.email})
    return _issue_tokens(response, db, user, request)


# ---- SSO (OIDC) ------------------------------------------------------------

@router.get("/auth/oidc/login")
def oidc_login():
    if not settings.oidc_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SSO is not configured")
    state = oidc.new_state()
    nonce = oidc.new_state()
    response = RedirectResponse(oidc.authorization_url(state, nonce))
    response.set_cookie(
        OIDC_COOKIE,
        oidc.state_cookie(state, nonce),
        max_age=600, httponly=True, secure=settings.cookie_secure, samesite="lax", path="/auth",
    )
    return response


@router.get("/auth/oidc/callback")
def oidc_callback(request: Request, code: str = "", state: str = "",
                  error: str = "", db: Session = Depends(get_db)):
    if not settings.oidc_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SSO is not configured")
    if error:
        log.info("auth.oidc_denied", extra={"error": error[:200]})
        return RedirectResponse("/?sso=denied")
    cookie = request.cookies.get(OIDC_COOKIE)
    try:
        expected_state, nonce = oidc.verify_state_cookie(cookie, state)
        claims = oidc.claims_from_code(code, nonce)
    except (oidc.OIDCError, jwt.PyJWTError) as exc:
        log.warning("auth.oidc_failed", extra={"error": str(exc)[:300]})
        return RedirectResponse("/?sso=error")

    email = (claims.get("email") or "").lower()
    subject = str(claims.get("sub") or "")
    if not email or claims.get("email_verified") is False:
        log.warning("auth.oidc_failed", extra={"error": "email missing or unverified"})
        return RedirectResponse("/?sso=error")

    user = db.scalar(select(User).where(User.oidc_subject == subject)) if subject else None
    if user is None:
        user = db.scalar(select(User).where(User.email == email))
    if user is None and settings.oidc_auto_provision:
        role = settings.oidc_default_role if settings.oidc_default_role in ROLES else "interviewer"
        groups = claims.get("groups") or []
        if settings.oidc_admin_group and settings.oidc_admin_group in groups:
            role = "hr_admin"
        user = User(email=email, name=str(claims.get("name") or email.split("@")[0]),
                    password_hash=hash_password(new_opaque_token()), role=role, oidc_subject=subject)
        db.add(user)
        db.commit()
        log.info("auth.oidc_provisioned", extra={"user": email, "role": role})
    if user is None or not user.is_active:
        log.info("auth.oidc_denied", extra={"user": email})
        return RedirectResponse("/?sso=denied")
    if subject and not user.oidc_subject:
        user.oidc_subject = subject
        db.commit()

    response = RedirectResponse("/?sso=ok")
    response.delete_cookie(OIDC_COOKIE, path="/auth")
    _successful_login(db, user)
    _issue_tokens(response, db, user, request)
    log.info("auth.login_ok", extra={"user": user.email, "role": user.role, "sso": True})
    return response


# ---- users (HR) ------------------------------------------------------------

@router.get("/auth/me")
def me(user: User = Depends(get_current_user)):
    return user_out(user)


@router.get("/users")
def list_users(db: Session = Depends(get_db), _: User = Depends(require_hr)):
    return [user_out(u) for u in db.scalars(select(User).order_by(User.id))]


@router.post("/users", status_code=201)
def create_user(body: UserIn, db: Session = Depends(get_db), _: User = Depends(require_hr)):
    if body.role not in ROLES:
        raise HTTPException(400, f"Role must be one of {ROLES}")
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "A user with that email already exists")
    user = User(email=email, name=body.name.strip(), password_hash=hash_password(body.password),
                role=body.role)
    db.add(user)
    db.commit()
    log.info("auth.user_created", extra={"user": email, "role": body.role})
    return user_out(user)


@router.post("/users/{user_id}/deactivate")
def deactivate_user(user_id: int, db: Session = Depends(get_db), me_: User = Depends(require_hr)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if user.id == me_.id:
        raise HTTPException(400, "You cannot deactivate yourself")
    if user.role == "hr_admin" and user.is_active:
        other_admins = db.scalar(
            select(User.id).where(User.role == "hr_admin", User.is_active.is_(True),
                                  User.id != user.id).limit(1)
        )
        if other_admins is None:
            raise HTTPException(400, "There must be at least one active HR admin")
    user.is_active = False
    user.token_version += 1
    db.commit()
    _revoke_all(db, user.id)
    log.info("auth.user_deactivated", extra={"user": user.email})
    return user_out(user)


@router.post("/users/{user_id}/password-reset", status_code=201)
def admin_password_reset(user_id: int, request: Request, db: Session = Depends(get_db),
                         me_: User = Depends(require_hr)):
    """HR hands a one-time link to a user through a trusted channel (chat, in person)."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    raw = new_opaque_token()
    expires = utcnow() + timedelta(minutes=settings.password_reset_minutes)
    db.add(PasswordReset(user_id=user.id, token_hash=hash_token(raw), expires_at=expires,
                         created_by=me_.id))
    db.commit()
    base = settings.public_base_url or str(request.base_url).rstrip("/")
    log.info("auth.password_reset_issued", extra={"user": user.email, "by": me_.email})
    return {
        "user": user_out(user),
        "reset_token": raw,
        "reset_url": f"{base}/?reset={raw}",
        "expires_at": expires.isoformat(timespec="minutes"),
    }
