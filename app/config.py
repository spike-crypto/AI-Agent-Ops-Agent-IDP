"""Settings, read from environment variables. Nothing secret lives in the repo.

`.env` is loaded for local runs only (Docker/Render inject the same keys as real env
vars). A missing or placeholder JWT_SECRET is a hard startup error: a random secret
per process silently signs everyone out on every deploy and makes tokens unverifiable
across workers.
"""

import os

from dotenv import load_dotenv

load_dotenv()

_PLACEHOLDER_SECRETS = {
    "change-me",
    "change-me-to-a-long-random-string",
    "changeme",
    "secret",
    "your-secret-here",
}


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from None


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.app_env = _env("APP_ENV", "development").lower()
        self.is_production = self.app_env == "production"
        self.company_name = _env("COMPANY_NAME", "Hiring Desk")
        # Render sets RENDER_EXTERNAL_URL automatically; PUBLIC_BASE_URL wins for custom domains.
        self.public_base_url = (
            _env("PUBLIC_BASE_URL") or _env("RENDER_EXTERNAL_URL", "") or ""
        ).rstrip("/")

        self.database_url = _env("DATABASE_URL", "sqlite:///./data/hr.db")

        self.jwt_secret = _env("JWT_SECRET")
        if not self.jwt_secret or self.jwt_secret.lower() in _PLACEHOLDER_SECRETS:
            raise RuntimeError(
                "JWT_SECRET is not set (or is a placeholder). Generate one with\n"
                "  python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
                "and put it in .env locally and in Render > Environment in production."
            )
        if len(self.jwt_secret) < 32:
            raise RuntimeError("JWT_SECRET must be at least 32 characters.")
        # Short-lived access tokens; the refresh token (httpOnly cookie) keeps people signed in.
        self.jwt_expiry_minutes = _int("JWT_EXPIRY_MINUTES", 15)
        self.refresh_token_days = _int("REFRESH_TOKEN_DAYS", 14)
        self.mfa_token_minutes = _int("MFA_TOKEN_MINUTES", 5)
        self.password_reset_minutes = _int("PASSWORD_RESET_MINUTES", 30)

        # Login protection. The account lockout is stored on the user row (works across
        # workers); the IP limiter is in-process (see app/ratelimit.py).
        self.login_max_attempts = _int("LOGIN_MAX_ATTEMPTS", 5)
        self.login_window_minutes = _int("LOGIN_WINDOW_MINUTES", 15)
        self.lockout_minutes = _int("LOCKOUT_MINUTES", 15)

        self.openrouter_api_key = _env("OPENROUTER_API_KEY")
        # First model is the main one; the rest are fallbacks. Free models are flaky,
        # so a model that times out or rate-limits is skipped for the next one.
        self.openrouter_models = [
            m.strip()
            for m in _env(
                "OPENROUTER_MODELS",
                "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free,"
                "google/gemma-4-26b-a4b-it:free,"
                "google/gemma-4-31b-it:free,"
                "nvidia/nemotron-3.5-lightning:free",
            ).split(",")
            if m.strip()
        ]
        self.llm_timeout_seconds = _int("LLM_TIMEOUT_SECONDS", 25)
        self.llm_max_retries = _int("LLM_MAX_RETRIES", 2)
        self.llm_max_tokens = _int("LLM_MAX_TOKENS", 1200)
        self.llm_concurrency = _int("LLM_CONCURRENCY", 3)
        self.agent_job_max_candidates = _int("AGENT_JOB_MAX_CANDIDATES", 50)
        self.run_worker = _bool("RUN_WORKER", True)
        self.worker_poll_seconds = float(_env("WORKER_POLL_SECONDS", "1.0"))

        # Langfuse tracing. Off unless both keys are set.
        self.langfuse_public_key = _env("LANGFUSE_PUBLIC_KEY")
        self.langfuse_secret_key = _env("LANGFUSE_SECRET_KEY")
        self.langfuse_host = (_env("LANGFUSE_HOST", "https://cloud.langfuse.com") or "").rstrip("/")
        # False = send timings, models and token counts only, never prompt or answer text.
        self.langfuse_capture_content = _bool("LANGFUSE_CAPTURE_CONTENT", True)

        # Optional single sign-on. Leave OIDC_ISSUER empty to disable the SSO button.
        self.oidc_issuer = (_env("OIDC_ISSUER", "") or "").rstrip("/")
        self.oidc_client_id = _env("OIDC_CLIENT_ID", "")
        self.oidc_client_secret = _env("OIDC_CLIENT_SECRET", "")
        self.oidc_redirect_uri = _env("OIDC_REDIRECT_URI", "")
        self.oidc_button_label = _env("OIDC_BUTTON_LABEL", "Sign in with SSO")
        self.oidc_auto_provision = _bool("OIDC_AUTO_PROVISION", False)
        self.oidc_default_role = _env("OIDC_DEFAULT_ROLE", "interviewer")
        self.oidc_admin_group = _env("OIDC_ADMIN_GROUP", "")

        self.mfa_issuer = _env("MFA_ISSUER", "Hiring Desk")
        self.cookie_secure = _bool("COOKIE_SECURE", self.is_production)
        self.public_apply_per_hour = _int("PUBLIC_APPLY_PER_HOUR", 5)

        self.log_level = _env("LOG_LEVEL", "INFO").upper()
        self.log_json = _bool("LOG_JSON", False)

        # First HR admin, created on startup if no users exist yet.
        self.admin_email = _env("ADMIN_EMAIL")
        self.admin_password = _env("ADMIN_PASSWORD")
        self.admin_name = _env("ADMIN_NAME", "HR Admin")

    @property
    def llm_configured(self) -> bool:
        return bool(self.openrouter_api_key)

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret)


settings = Settings()
