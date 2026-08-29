import asyncio
import uuid

import fakeredis.aioredis
import pytest

from app.core.config import get_settings
from app.services.rate_limiter import RateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_allows_under_limit() -> None:
    limiter = RateLimiter()
    user_id = uuid.uuid4()
    limit = get_settings().rate_limit_per_user_per_minute

    for _ in range(limit):
        allowed, msg = await limiter.check_rate_limit(user_id)
        assert allowed is True
        assert msg is None


@pytest.mark.asyncio
async def test_rate_limiter_blocks_on_user_limit_exceeded() -> None:
    limiter = RateLimiter()
    user_id = uuid.uuid4()
    limit = get_settings().rate_limit_per_user_per_minute

    # Consume all slots
    for _ in range(limit):
        await limiter.check_rate_limit(user_id)

    # limit + 1 request must be blocked
    allowed, msg = await limiter.check_rate_limit(user_id)
    assert allowed is False
    assert "User rate limit exceeded" in str(msg)


@pytest.mark.asyncio
async def test_ip_rate_limit_allows_under_limit() -> None:
    limiter = RateLimiter()
    ip = "192.168.1.100"
    limit = get_settings().rate_limit_per_ip_per_minute

    for _ in range(limit):
        allowed, msg = await limiter.check_ip_rate_limit(ip)
        assert allowed is True
        assert msg is None


@pytest.mark.asyncio
async def test_ip_rate_limit_blocks_on_exceeded() -> None:
    limiter = RateLimiter()
    ip = "10.0.0.1"
    limit = get_settings().rate_limit_per_ip_per_minute

    # Consume all slots
    for _ in range(limit):
        await limiter.check_ip_rate_limit(ip)

    # limit + 1 request must be blocked
    allowed, msg = await limiter.check_ip_rate_limit(ip)
    assert allowed is False
    assert "IP rate limit exceeded" in str(msg)


@pytest.mark.asyncio
async def test_redis_ip_rate_limit_concurrency() -> None:
    """Redis-backed concurrency test running simultaneous checks.

    Verifies atomicity of the Lua script: exactly limit succeed and remainder fail.
    """
    fake_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    limiter = RateLimiter(redis_client=fake_client)
    ip = "203.0.113.42"
    limit = get_settings().rate_limit_per_ip_per_minute

    # Launch concurrent checks
    tasks = [limiter.check_ip_rate_limit(ip) for _ in range(limit + 10)]
    results = await asyncio.gather(*tasks)

    allowed_count = sum(1 for allowed, _ in results if allowed)
    blocked_count = sum(1 for allowed, _ in results if not allowed)

    assert allowed_count == limit
    assert blocked_count == 10
    for allowed, msg in results:
        if not allowed:
            assert "IP rate limit exceeded" in str(msg)
