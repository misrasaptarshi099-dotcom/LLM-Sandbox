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


@pytest.mark.asyncio
async def test_outage_and_recovery_preserves_usage() -> None:
    """Usage recorded during Redis outage is merged into Redis totals upon recovery."""
    from unittest.mock import patch

    import fakeredis.aioredis

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    tracker = CostTracker(redis_client=fake_redis)

    # 1. Normal operation with Redis: record 100 input, 50 output
    await tracker.record_usage(input_tokens=100, output_tokens=50)
    summary1 = await tracker.get_usage_summary()
    assert summary1["input_tokens"] == 100
    assert summary1["output_tokens"] == 50

    # 2. Simulate Redis outage: pipeline.execute raises ConnectionError
    with patch.object(fake_redis, "pipeline", side_effect=ConnectionError("Redis down")):
        await tracker.record_usage(input_tokens=200, output_tokens=80)
        assert tracker._mem_input_tokens == 200
        assert tracker._mem_output_tokens == 80
        assert tracker._pending_input_sync == 200
        assert tracker._pending_output_sync == 80

    # 3. Redis recovers: get_usage_summary merges pending ledger into Redis
    summary2 = await tracker.get_usage_summary()
    assert summary2["input_tokens"] == 300  # 100 + 200
    assert summary2["output_tokens"] == 130  # 50 + 80

    # 4. Verify Redis keys themselves now hold the merged values
    assert int(await fake_redis.get("budget:input_tokens")) == 300
    assert int(await fake_redis.get("budget:output_tokens")) == 130

    # 5. Further usage continues to record properly
    await tracker.record_usage(input_tokens=50, output_tokens=20)
    summary3 = await tracker.get_usage_summary()
    assert summary3["input_tokens"] == 350
    assert summary3["output_tokens"] == 150


@pytest.mark.asyncio
async def test_reserve_and_reconcile_flow() -> None:
    """Reservations hold budget and reconcile against actual usage."""
    from unittest.mock import patch

    import fakeredis.aioredis

    from app.core.config import Settings

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    tracker = CostTracker(redis_client=fake_redis)

    small_settings = Settings(event_max_input_tokens=1000, event_max_output_tokens=500)
    with patch("app.services.cost_tracker.get_settings", return_value=small_settings):
        # 1. Reserve 600 input, 300 output -> succeeds
        ok, _ = await tracker.reserve_tokens(max_input_tokens=600, max_output_tokens=300)
        assert ok is True

        # 2. Second reservation for 500 input -> 600 + 500 = 1100 > 1000 -> fails
        ok2, msg2 = await tracker.reserve_tokens(max_input_tokens=500, max_output_tokens=100)
        assert ok2 is False
        assert "input token budget exhausted" in msg2.lower()

        # 3. Reconcile first run: actual was only 200 input, 100 output
        await tracker.reconcile_reservation(
            reserved_input_tokens=600,
            reserved_output_tokens=300,
            actual_input_tokens=200,
            actual_output_tokens=100,
        )

        # 4. Now 200 used, 0 reserved. Second reservation for 500 now succeeds (700 <= 1000)
        ok3, _ = await tracker.reserve_tokens(max_input_tokens=500, max_output_tokens=100)
        assert ok3 is True
