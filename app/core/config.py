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
    postgres_user: str | None = None
    postgres_password: str | None = None
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str | None = None

    @model_validator(mode="after")
    def assemble_database_url(self) -> Settings:
        """Safely encode credentials when discrete postgres parameters are provided."""
        if self.postgres_user and self.postgres_password and self.postgres_db:
            from urllib.parse import quote_plus

            safe_user = quote_plus(self.postgres_user)
            safe_pass = quote_plus(self.postgres_password)
            self.database_url = (
                f"postgresql+asyncpg://{safe_user}:{safe_pass}@"
                f"{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
            )
        return self

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- Encryption ---
    aes_256_gcm_secret: str = _DEFAULT_SECRET_PLACEHOLDER

    # --- Auth ---
    dev_auth_token: str = _DEFAULT_AUTH_TOKEN

    # --- LLM Providers ---
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    gemini_api_key: str | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    ollama_base_url: str = "http://localhost:11434"
    llm_model: str = "gemini-3.5-flash-lite"
    provider_timeout_seconds: float = 30.0
    provider_allowed_hosts: list[str] = [
        "api.openai.com",
        "generativelanguage.googleapis.com",
        "api.groq.com",
        "api.together.xyz",
        "api.deepseek.com",
        "openrouter.ai",
        "localhost",
        "127.0.0.1",
    ]

    # --- Rate Limiting & Network ---
    rate_limit_per_user_per_minute: int = 30
    rate_limit_per_ip_per_minute: int = 60
    rate_limit_global_per_minute: int = 300
    trusted_proxies: list[str] = ["127.0.0.1", "::1"]

    # --- Admission Control & Size Limits ---
    max_queue_depth: int = 1000
    max_request_body_bytes: int = 65_536

    # --- Provider Concurrency ---
    provider_max_concurrency: int = 10

    # --- Event-Wide Token Budget ---
    event_max_input_tokens: int = 10_000_000
    event_max_output_tokens: int = 5_000_000

    # --- Application ---
    app_env: str = "development"
    log_level: str = "INFO"
    embedded_worker: bool = False

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
