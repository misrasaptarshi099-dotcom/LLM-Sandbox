"""Unit tests for configuration validation and secret safety."""

from __future__ import annotations

import pytest

from app.core.config import Settings, get_settings


def test_dev_environment_allows_default_secrets() -> None:
    settings = Settings(app_env="development")
    assert settings.app_env == "development"


def test_production_environment_rejects_default_secret_key() -> None:
    with pytest.raises(ValueError, match="Production environment requires a secure"):
        Settings(
            app_env="production",
            dev_auth_token="secure-prod-token",
        )


def test_production_environment_rejects_default_dev_auth_token() -> None:
    with pytest.raises(ValueError, match="Production environment requires a secure"):
        Settings(
            app_env="production",
            aes_256_gcm_secret="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        )


def test_production_environment_accepts_secure_secrets() -> None:
    settings = Settings(
        app_env="production",
        aes_256_gcm_secret="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        dev_auth_token="my-secure-production-auth-token-123",
    )
    assert settings.app_env == "production"


def test_get_settings_is_cached() -> None:
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
