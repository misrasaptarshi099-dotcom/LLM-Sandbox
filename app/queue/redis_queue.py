"""Production Redis queue implementation using Redis Lists/Streams."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.queue.base import AbstractQueue

_DEFAULT_QUEUE_KEY = "llm_sandbox:queue:runs"


class RedisQueue(AbstractQueue):
    """Reliable queue producer and consumer powered by Redis."""

    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        queue_key: str = _DEFAULT_QUEUE_KEY,
    ) -> None:
        self.queue_key = queue_key
        self._client = redis_client or aioredis.from_url(
            get_settings().redis_url,
            decode_responses=True,
        )

    async def enqueue(self, run_id: uuid.UUID, metadata: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "run_id": str(run_id),
            "attempt": 1,
            "created_at": datetime.now(UTC).isoformat(),
        }
        if metadata:
            payload.update(metadata)
        await self._client.rpush(self.queue_key, json.dumps(payload))

    async def dequeue(self, timeout_seconds: float = 1.0) -> dict[str, Any] | None:
        result = await self._client.blpop(self.queue_key, timeout=int(max(1.0, timeout_seconds)))
        if not result:
            return None
        _, raw_json = result
        return json.loads(raw_json)

    async def qsize(self) -> int:
        return await self._client.llen(self.queue_key)

    async def close(self) -> None:
        await self._client.aclose()
