import asyncio
import uuid

import fakeredis.aioredis
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


@pytest.mark.asyncio
async def test_ip_rate_limit_allows_under_limit() -> None:
    limiter = RateLimiter()
    ip = "192.168.1.100"

    for _ in range(60):
        allowed, msg = await limiter.check_ip_rate_limit(ip)
        assert allowed is True
        assert msg is None


@pytest.mark.asyncio
async def test_ip_rate_limit_blocks_on_exceeded() -> None:
    limiter = RateLimiter()
    ip = "10.0.0.1"

    # Consume 60 slots (default per-IP limit)
    for _ in range(60):
        await limiter.check_ip_rate_limit(ip)

    # 61st request must be blocked
    allowed, msg = await limiter.check_ip_rate_limit(ip)
    assert allowed is False
    assert "IP rate limit exceeded" in str(msg)


@pytest.mark.asyncio
async def test_redis_ip_rate_limit_concurrency() -> None:
    """Redis-backed concurrency test running > 60 simultaneous checks.

    Verifies atomicity of the Lua script: exactly 60 succeed and remainder fail.
    """
    fake_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    limiter = RateLimiter(redis_client=fake_client)
    ip = "203.0.113.42"

    # Launch 80 concurrent checks
    tasks = [limiter.check_ip_rate_limit(ip) for _ in range(80)]
    results = await asyncio.gather(*tasks)

    allowed_count = sum(1 for allowed, _ in results if allowed)
    blocked_count = sum(1 for allowed, _ in results if not allowed)

    assert allowed_count == 60
    assert blocked_count == 20
    for allowed, msg in results:
        if not allowed:
            assert "IP rate limit exceeded" in str(msg)
