"""Unit tests for structured logging and recursive secret redaction."""

from __future__ import annotations

import json
import logging

from app.core.logging import StructuredJsonFormatter, _redact


def test_redact_nested_dict_and_sequences() -> None:
    payload = {
        "user_id": "123",
        "api_key": "sk-secret123",
        "nested": {
            "token": "secret-token",
            "safe_list": ["item1", "item2"],
            "secret_list": [
                {"authorization": "Bearer secret_jwt"},
                {"safe_field": "ok"},
            ],
        },
    }
    redacted = _redact(payload)

    assert redacted["api_key"] == "**REDACTED**"
    assert redacted["user_id"] == "123"
    assert redacted["nested"]["token"] == "**REDACTED**"
    assert redacted["nested"]["safe_list"] == ["item1", "item2"]
    assert redacted["nested"]["secret_list"][0]["authorization"] == "**REDACTED**"
    assert redacted["nested"]["secret_list"][1]["safe_field"] == "ok"


def test_structured_formatter_redacts_messages() -> None:
    formatter = StructuredJsonFormatter()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Handling request with Bearer secret-auth-token-xyz",
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record)
    parsed = json.loads(formatted)

    assert "secret-auth-token-xyz" not in formatted
    assert parsed["message"] == "Handling request with Bearer **REDACTED**"
