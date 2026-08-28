"""Application configuration — loaded from environment variables.

Uses pydantic-settings to parse .env / Railway injected env vars.
No secrets are logged or printed (Rule §2).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    aes_256_gcm_secret: str = "CHANGE_ME_generate_a_64_hex_char_secret_key_here"

    # --- Auth ---
    dev_auth_token: str = "dev-token"

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

    def __repr__(self) -> str:
        """Never expose secrets in repr (Rule §2)."""
        return "Settings(**REDACTED**)"


def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
