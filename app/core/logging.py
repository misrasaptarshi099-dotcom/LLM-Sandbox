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
_REDACTED_KEYS = frozenset(
    {
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
    }
)


def _redact_string(text: str) -> str:
    """Redact bearer token / secret string patterns if present in strings."""
    if "Bearer " in text:
        parts = text.split("Bearer ")
        return parts[0] + "Bearer **REDACTED**"
    return text


def _redact_value(val: Any) -> Any:
    """Recursively redact nested data structures and string/object representations."""
    if isinstance(val, dict):
        return _redact_dict(val)
    if isinstance(val, (list, tuple, set)):
        return [_redact_value(item) for item in val]
    if isinstance(val, (int, float, bool, type(None))):
        return val
    if isinstance(val, str):
        return _redact_string(val)
    # For custom non-scalar objects, convert to string representation and redact
    return _redact_string(str(val))


def _redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact sensitive keys in a dictionary."""
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and key.lower() in _REDACTED_KEYS:
            cleaned[key] = "**REDACTED**"
        else:
            cleaned[key] = _redact_value(value)
    return cleaned


def _redact(data: Any) -> Any:
    """Entrypoint for recursive redaction."""
    return _redact_value(data)


class StructuredJsonFormatter(logging.Formatter):
    """Emit one JSON object per log line with automatic redaction."""

    def format(self, record: logging.LogRecord) -> str:
        safe_message = _redact_string(record.getMessage())
        log_entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": safe_message,
        }
        # Merge any extra structured fields attached to the record after redaction.
        if hasattr(record, "extra_fields"):
            log_entry.update(_redact(record.extra_fields))
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = _redact_string(str(record.exc_info[1]))
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
