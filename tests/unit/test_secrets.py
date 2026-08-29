"""Unit tests for secret manager."""

from __future__ import annotations

import pytest

from app.core.secrets import EnvSecretManager, get_secret_manager, set_secret_manager


def test_env_secret_manager_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("TEST_SECRET_KEY", "super_secret_value_123")
    manager = EnvSecretManager()

    assert manager.get_secret("TEST_SECRET_KEY") == "super_secret_value_123"
    assert manager.require_secret("TEST_SECRET_KEY") == "super_secret_value_123"


def test_env_secret_manager_falls_back_to_settings(monkeypatch) -> None:
    # Ensure env var is absent so it falls back to Settings
    monkeypatch.delenv("APP_ENV", raising=False)
    manager = EnvSecretManager()

    assert manager.get_secret("APP_ENV") == "development"


def test_env_secret_manager_missing_secret_returns_default() -> None:
    manager = EnvSecretManager()
    assert manager.get_secret("NON_EXISTENT_KEY_XYZ", default="fallback") == "fallback"
    assert manager.get_secret("NON_EXISTENT_KEY_XYZ") is None


def test_env_secret_manager_require_missing_raises_key_error() -> None:
    manager = EnvSecretManager()
    with pytest.raises(KeyError, match="Required secret 'TOTALLY_MISSING'"):
        manager.require_secret("TOTALLY_MISSING")


def test_get_and_set_global_secret_manager() -> None:
    custom_manager = EnvSecretManager()
    set_secret_manager(custom_manager)
    assert get_secret_manager() is custom_manager
