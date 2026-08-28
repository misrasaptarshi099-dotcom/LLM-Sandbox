"""Unit tests for sliding-window rate limiter."""

from __future__ import annotations

import uuid

import pytest

from app.services.rate_limiter import RateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_allows_under_limit() -> None:
    limiter = RateLimiter()
    user_id = uuid.uuid4()

    for _ in range(30):
        allowed, msg = await limiter.check_rate_limit(user_id)
        assert allowed is True
        assert msg is None


@pytest.mark.asyncio
async def test_rate_limiter_blocks_on_user_limit_exceeded() -> None:
    limiter = RateLimiter()
    user_id = uuid.uuid4()

    # Consume 30 slots
    for _ in range(30):
        await limiter.check_rate_limit(user_id)

    # 31st request must be blocked
    allowed, msg = await limiter.check_rate_limit(user_id)
    assert allowed is False
    assert "User rate limit exceeded" in str(msg)
