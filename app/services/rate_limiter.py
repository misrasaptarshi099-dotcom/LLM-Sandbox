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


class RateLimiter:
    """Sliding-window counter rate limiter."""

    def __init__(self, redis_client: aioredis.Redis | None = None) -> None:
        self.redis = redis_client
        self._memory_buckets: dict[str, list[float]] = defaultdict(list)

    async def check_rate_limit(self, user_id: uuid.UUID) -> tuple[bool, str | None]:
        """Check if user or global rate limit is exceeded.

        Returns (allowed: bool, retry_after: str | None).
        """
        settings = get_settings()
        user_limit = settings.rate_limit_per_user_per_minute
        global_limit = settings.rate_limit_global_per_minute
        now = time.time()
        window = 60.0

        if self.redis is not None:
            try:
                user_key = f"ratelimit:user:{user_id}"
                global_key = "ratelimit:global"

                # Check user limit
                pipe = self.redis.pipeline()
                pipe.zremrangebyscore(user_key, 0, now - window)
                pipe.zcard(user_key)
                pipe.zremrangebyscore(global_key, 0, now - window)
                pipe.zcard(global_key)
                _, user_count, _, global_count = await pipe.execute()

                if user_count >= user_limit:
                    return False, "User rate limit exceeded (30 req/min). Please try again shortly."
                if global_count >= global_limit:
                    return False, "Global system capacity reached. Please try again shortly."

                # Record attempt
                pipe2 = self.redis.pipeline()
                pipe2.zadd(user_key, {str(now): now})
                pipe2.expire(user_key, int(window) + 5)
                pipe2.zadd(global_key, {str(now): now})
                pipe2.expire(global_key, int(window) + 5)
                await pipe2.execute()
                return True, None
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
