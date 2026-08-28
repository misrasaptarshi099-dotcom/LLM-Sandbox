"""Structured JSON logging with strict redaction.

Rules §2, §10:
- Never log system prompts, API keys, Authorization headers, or raw payloads.
- Logs are structured JSON with correlation IDs.
- Redaction is applied before emission.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

# Keys whose values must NEVER appear in logs.
_REDACTED_KEYS = frozenset({
    "authorization",
    "api_key",
    "api-key",
    "openai_api_key",
    "aes_256_gcm_secret",
    "system_prompt",
    "system_prompt_ciphertext",
    "password",
    "secret",
    "token",
    "dev_auth_token",
})


def _redact(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact sensitive keys in a dict."""
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in _REDACTED_KEYS:
            cleaned[key] = "**REDACTED**"
        elif isinstance(value, dict):
            cleaned[key] = _redact(value)
        else:
            cleaned[key] = value
    return cleaned


class StructuredJsonFormatter(logging.Formatter):
    """Emit one JSON object per log line with automatic redaction."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Merge any extra structured fields attached to the record.
        if hasattr(record, "extra_fields"):
            log_entry.update(_redact(record.extra_fields))
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = str(record.exc_info[1])
        return json.dumps(log_entry, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger with structured JSON output and redaction."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredJsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Suppress noisy third-party loggers.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger."""
    return logging.getLogger(f"llm_sandbox.{name}")
