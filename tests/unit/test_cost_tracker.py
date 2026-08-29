"""Unit tests for event-wide token budget tracker (Phase 7)."""

from __future__ import annotations

import pytest

from app.services.cost_tracker import CostTracker


@pytest.mark.asyncio
async def test_budget_allows_under_limit() -> None:
    """Usage under cap passes budget check."""
    tracker = CostTracker()
    # Record some usage well under defaults (10M input, 5M output)
    await tracker.record_usage(input_tokens=1000, output_tokens=500)

    allowed, msg = await tracker.check_budget()
    assert allowed is True
    assert msg is None


@pytest.mark.asyncio
async def test_budget_blocks_on_input_exhausted(monkeypatch) -> None:
    """Input token cap → rejection."""
    from unittest.mock import patch

    from app.core.config import Settings

    small_budget_settings = Settings(event_max_input_tokens=100)
    with patch("app.services.cost_tracker.get_settings", return_value=small_budget_settings):
        tracker = CostTracker()
        await tracker.record_usage(input_tokens=100, output_tokens=0)

        allowed, msg = await tracker.check_budget()
        assert allowed is False
        assert "input token budget exhausted" in msg.lower()


@pytest.mark.asyncio
async def test_budget_blocks_on_output_exhausted(monkeypatch) -> None:
    """Output token cap → rejection."""
    from unittest.mock import patch

    from app.core.config import Settings

    small_budget_settings = Settings(event_max_output_tokens=50)
    with patch("app.services.cost_tracker.get_settings", return_value=small_budget_settings):
        tracker = CostTracker()
        await tracker.record_usage(input_tokens=0, output_tokens=50)

        allowed, msg = await tracker.check_budget()
        assert allowed is False
        assert "output token budget exhausted" in msg.lower()


@pytest.mark.asyncio
async def test_record_and_summary() -> None:
    """Atomic increment correctness."""
    tracker = CostTracker()
    await tracker.record_usage(input_tokens=100, output_tokens=50)
    await tracker.record_usage(input_tokens=200, output_tokens=75)

    summary = await tracker.get_usage_summary()
    assert summary["input_tokens"] == 300
    assert summary["output_tokens"] == 125


@pytest.mark.asyncio
async def test_negative_usage_ignored() -> None:
    """Negative token counts should not change budget."""
    tracker = CostTracker()
    await tracker.record_usage(input_tokens=-10, output_tokens=-5)

    summary = await tracker.get_usage_summary()
    assert summary["input_tokens"] == 0
    assert summary["output_tokens"] == 0
