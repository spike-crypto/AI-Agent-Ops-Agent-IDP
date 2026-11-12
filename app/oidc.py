"""Optional OIDC single sign-on (Google Workspace, Entra ID, Keycloak, Auth0...).

Disabled unless OIDC_ISSUER, OIDC_CLIENT_ID and OIDC_CLIENT_SECRET are set.
SAML is not implemented directly; Keycloak or Entra can broker SAML into OIDC.
"""

import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import urlencode

import httpx
import jwt

from app.config import settings


class OIDCError(Exception):
    pass


def redirect_uri() -> str:
    uri = settings.oidc_redirect_uri or (
        f"{settings.public_base_url}/auth/oidc/callback" if settings.public_base_url else ""
    )
    if not uri:
        raise OIDCError("Set OIDC_REDIRECT_URI (or PUBLIC_BASE_URL) so the provider knows where to return")
    return uri


@lru_cache
def discovery() -> dict:
    url = f"{settings.oidc_issuer}/.well-known/openid-configuration"
    try:
        response = httpx.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise OIDCError(f"Could not read OIDC discovery from {url}: {exc}") from exc
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
        if not data.get(key):
            raise OIDCError(f"OIDC discovery is missing {key}")
    return data


@lru_cache
def _jwks() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(discovery()["jwks_uri"])


def new_state() -> str:
    return secrets.token_urlsafe(24)


def authorization_url(state: str, nonce: str) -> str:
    query = urlencode({
        "client_id": settings.oidc_client_id,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": redirect_uri(),
        "state": state,
        "nonce": nonce,
    })
    return f"{discovery()['authorization_endpoint']}?{query}"


def state_cookie(state: str, nonce: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"scope": "oidc_state", "state": state, "nonce": nonce,
         "iat": now, "exp": now + timedelta(minutes=10)},
        settings.jwt_secret, algorithm="HS256",
    )


def verify_state_cookie(cookie: str | None, state: str) -> tuple[str, str]:
    if not cookie:
        raise OIDCError("Missing SSO state cookie (the sign-in took too long or cookies are blocked)")
    try:
        payload = jwt.decode(cookie, settings.jwt_secret, algorithms=["HS256"],
                             options={"require": ["exp", "state", "nonce", "scope"]})
    except jwt.PyJWTError as exc:
        raise OIDCError("SSO state expired. Try signing in again.") from exc
    if payload.get("scope") != "oidc_state" or not secrets.compare_digest(payload["state"], state):
        raise OIDCError("SSO state did not match")
    return payload["state"], payload["nonce"]


def claims_from_code(code: str, nonce: str) -> dict:
    if not code:
        raise OIDCError("The provider did not return a code")
    try:
        response = httpx.post(
            discovery()["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri(),
                "client_id": settings.oidc_client_id,
                "client_secret": settings.oidc_client_secret,
            },
            timeout=15,
        )
        response.raise_for_status()
        tokens = response.json()
    except Exception as exc:
        raise OIDCError(f"Token exchange failed: {exc}") from exc

    id_token = tokens.get("id_token")
    if not id_token:
        raise OIDCError("The provider returned no id_token")
    try:
        signing_key = _jwks().get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=discovery().get("id_token_signing_alg_values_supported") or ["RS256"],
            audience=settings.oidc_client_id,
            issuer=discovery()["issuer"],
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise OIDCError(f"Could not verify the id_token: {exc}") from exc
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OIDCError("id_token nonce did not match")
    return claims
