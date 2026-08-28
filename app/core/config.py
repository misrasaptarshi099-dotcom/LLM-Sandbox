"""Application configuration — loaded from environment variables.

Uses pydantic-settings to parse .env / Railway injected env vars.
No secrets are logged or printed (Rule §2).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_SECRET_PLACEHOLDER = "CHANGE_ME_generate_a_64_hex_char_secret_key_here"
_DEFAULT_AUTH_TOKEN = "dev-token"
_DEV_ENVS = frozenset({"development", "dev", "test", "testing"})


class Settings(BaseSettings):
    """Immutable application settings sourced from environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database ---
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/llm_sandbox"

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Encryption ---
    aes_256_gcm_secret: str = _DEFAULT_SECRET_PLACEHOLDER

    # --- Auth ---
    dev_auth_token: str = _DEFAULT_AUTH_TOKEN

    # --- LLM Provider (optional — FakeLLMProvider used when not set) ---
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    # --- Rate Limiting ---
    rate_limit_per_user_per_minute: int = 30
    rate_limit_global_per_minute: int = 300

    # --- Application ---
    app_env: str = "development"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_production_secrets(self) -> Settings:
        """Ensure production environments cannot start with insecure default secrets (Rule §2)."""
        if self.app_env.lower() not in _DEV_ENVS:
            if self.aes_256_gcm_secret == _DEFAULT_SECRET_PLACEHOLDER:
                raise ValueError(
                    "Production environment requires a secure, non-default AES_256_GCM_SECRET."
                )
            if self.dev_auth_token == _DEFAULT_AUTH_TOKEN:
                raise ValueError(
                    "Production environment requires a secure, non-default DEV_AUTH_TOKEN."
                )
        return self

    def __repr__(self) -> str:
        """Never expose secrets in repr (Rule §2)."""
        return "Settings(**REDACTED**)"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
