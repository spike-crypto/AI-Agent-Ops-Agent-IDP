"""Settings, read from environment variables. Nothing secret lives in the repo."""

import os
import secrets
import warnings


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


class Settings:
    def __init__(self) -> None:
        self.database_url = _env("DATABASE_URL", "sqlite:///./data/hr.db")

        self.jwt_secret = _env("JWT_SECRET")
        if not self.jwt_secret:
            # Fine for local dev: tokens just stop working when the app restarts.
            warnings.warn("JWT_SECRET is not set; using a random one for this process.")
            self.jwt_secret = secrets.token_urlsafe(32)
        self.jwt_expiry_minutes = int(_env("JWT_EXPIRY_MINUTES", "480"))

        self.openrouter_api_key = _env("OPENROUTER_API_KEY")
        # "fake" needs no key and gives deterministic answers; tests and CI always use it.
        self.llm_provider = _env("LLM_PROVIDER", "openrouter" if self.openrouter_api_key else "fake")
        # Comma-separated: the first is the main model, the rest are fallbacks.
        self.openrouter_models = [
            m.strip()
            for m in _env("OPENROUTER_MODELS", "google/gemma-4-31b-it:free,google/gemma-4-26b-a4b-it:free,nvidia/nemotron-3.5-lightning:free").split(",")
            if m.strip()
        ]

        self.agentops_api_key = _env("AGENTOPS_API_KEY")

        # First HR admin, created on startup if no users exist yet.
        self.admin_email = _env("ADMIN_EMAIL")
        self.admin_password = _env("ADMIN_PASSWORD")
        self.admin_name = _env("ADMIN_NAME", "HR Admin")


settings = Settings()
