"""Secret Management Abstraction.

Architecture §14, Rules §2, PRD §11:
- Provider credentials and platform secrets are accessed through a unified interface.
- Default implementation resolves from environment variables / config.
- Extensible for cloud secret managers (AWS Secrets Manager, GCP Secret Manager, Vault).
- Never exposes secret values in exception messages, string representations, or logs.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from app.core.config import get_settings


@runtime_checkable
class SecretManager(Protocol):
    """Protocol defining secret retrieval operations."""

    def get_secret(self, key: str, default: str | None = None) -> str | None:
        """Retrieve secret by name. Returns default if not found."""
        ...

    def require_secret(self, key: str) -> str:
        """Retrieve secret by name, raising KeyError if missing."""
        ...


class EnvSecretManager:
    """Environment-based secret manager with config fallback."""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    def get_secret(self, key: str, default: str | None = None) -> str | None:
        """Retrieve secret from environment or settings."""
        # 1. Direct environment variable lookup
        val = os.environ.get(key)
        if val is not None and val != "":
            return val

        # 2. Config settings fallback (case-insensitive attribute match)
        settings = get_settings()
        attr_name = key.lower()
        if hasattr(settings, attr_name):
            attr_val = getattr(settings, attr_name)
            if attr_val is not None and str(attr_val) != "":
                return str(attr_val)

        return default

    def require_secret(self, key: str) -> str:
        """Retrieve required secret or raise KeyError without exposing values."""
        val = self.get_secret(key)
        if val is None or val == "":
            raise KeyError(f"Required secret '{key}' is not configured.")
        return val


_default_secret_manager: SecretManager | None = None


def get_secret_manager() -> SecretManager:
    """Return the global secret manager instance."""
    global _default_secret_manager
    if _default_secret_manager is None:
        _default_secret_manager = EnvSecretManager()
    return _default_secret_manager


def set_secret_manager(manager: SecretManager) -> None:
    """Override the global secret manager (e.g. for testing)."""
    global _default_secret_manager
    _default_secret_manager = manager
