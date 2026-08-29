"""Sliding window rate limiter using Redis with an in-memory fallback.

Rules §4, PRD §6:
- 30 requests / minute / participant.
- 300 requests / minute / system global.
- Fast non-blocking check before admission.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.logging import get_logger

_LUA_RATE_LIMIT_SCRIPT = """
local user_key = KEYS[1]
local global_key = KEYS[2]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local user_limit = tonumber(ARGV[3])
local global_limit = tonumber(ARGV[4])
local member = ARGV[5]

local clear_before = now - window

-- Prune user
redis.call('ZREMRANGEBYSCORE', user_key, '-inf', clear_before)
local user_count = redis.call('ZCARD', user_key)
if user_count >= user_limit then
    return {0, 'USER_LIMIT'}
end

-- Prune global
redis.call('ZREMRANGEBYSCORE', global_key, '-inf', clear_before)
local global_count = redis.call('ZCARD', global_key)
if global_count >= global_limit then
    return {0, 'GLOBAL_LIMIT'}
end

-- Both allowed: record attempt and set TTL
redis.call('ZADD', user_key, now, member)
redis.call('EXPIRE', user_key, math.ceil(window) + 5)
redis.call('ZADD', global_key, now, member)
redis.call('EXPIRE', global_key, math.ceil(window) + 5)

return {1, 'OK'}
"""


class RateLimiter:
    """Sliding-window counter rate limiter."""

    def __init__(self, redis_client: aioredis.Redis | None = None) -> None:
        self.redis = redis_client
        self._memory_buckets: dict[str, list[float]] = defaultdict(list)
        self._lua_script = None

    async def check_ip_rate_limit(self, ip_address: str) -> tuple[bool, str | None]:
        """Check if per-IP rate limit is exceeded.

        Returns (allowed: bool, error_message: str | None).
        """
        settings = get_settings()
        ip_limit = settings.rate_limit_per_ip_per_minute
        now = time.time()
        window = 60.0
        member = f"{now}:{uuid.uuid4().hex}"

        if self.redis is not None:
            try:
                ip_key = f"ratelimit:ip:{ip_address}"
                # Prune + check + record in one round-trip
                pipe = self.redis.pipeline()
                pipe.zremrangebyscore(ip_key, "-inf", now - window)
                pipe.zcard(ip_key)
                results = await pipe.execute()
                ip_count = results[1]

                if ip_count >= ip_limit:
                    return False, "IP rate limit exceeded. Please try again shortly."

                pipe2 = self.redis.pipeline()
                pipe2.zadd(ip_key, {member: now})
                pipe2.expire(ip_key, int(window) + 5)
                await pipe2.execute()
                return True, None
            except Exception as exc:
                logger = get_logger("rate_limiter")
                logger.warning(
                    "Redis IP rate limiter unavailable, falling back to memory",
                    extra={"extra_fields": {"error": str(exc)}},
                )

        # In-memory fallback
        ip_key_mem = f"ip:{ip_address}"
        self._memory_buckets[ip_key_mem] = [
            t for t in self._memory_buckets[ip_key_mem] if t > now - window
        ]
        if len(self._memory_buckets[ip_key_mem]) >= ip_limit:
            return False, "IP rate limit exceeded. Please try again shortly."

        self._memory_buckets[ip_key_mem].append(now)
        return True, None

    async def check_rate_limit(self, user_id: uuid.UUID) -> tuple[bool, str | None]:
        """Check if user or global rate limit is exceeded.

        Returns (allowed: bool, retry_after: str | None).
        """
        settings = get_settings()
        user_limit = settings.rate_limit_per_user_per_minute
        global_limit = settings.rate_limit_global_per_minute
        now = time.time()
        window = 60.0
        member = f"{now}:{uuid.uuid4().hex}"

        if self.redis is not None:
            try:
                user_key = f"ratelimit:user:{user_id}"
                global_key = "ratelimit:global"

                res = await self.redis.eval(
                    _LUA_RATE_LIMIT_SCRIPT,
                    2,
                    user_key,
                    global_key,
                    str(now),
                    str(window),
                    str(user_limit),
                    str(global_limit),
                    member,
                )
                allowed, reason = res[0], res[1]
                if allowed == 1:
                    return True, None
                if reason == "USER_LIMIT":
                    return False, "User rate limit exceeded (30 req/min). Please try again shortly."
                return False, "Global system capacity reached. Please try again shortly."
            except Exception as exc:
                # Log redis exception and fallback to in-memory sliding window
                logger = get_logger("rate_limiter")
                logger.warning(f"Redis rate limiter unavailable, falling back to memory: {exc}")

        # In-memory sliding window fallback
        # User window
        user_key_mem = f"user:{user_id}"
        self._memory_buckets[user_key_mem] = [
            t for t in self._memory_buckets[user_key_mem] if t > now - window
        ]
        if len(self._memory_buckets[user_key_mem]) >= user_limit:
            return False, "User rate limit exceeded (30 req/min). Please try again shortly."

        # Global window
        global_key_mem = "global"
        self._memory_buckets[global_key_mem] = [
            t for t in self._memory_buckets[global_key_mem] if t > now - window
        ]
        if len(self._memory_buckets[global_key_mem]) >= global_limit:
            return False, "Global system capacity reached. Please try again shortly."

        # Record
        self._memory_buckets[user_key_mem].append(now)
        self._memory_buckets[global_key_mem].append(now)
        return True, None
