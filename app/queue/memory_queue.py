"""In-memory queue implementation for testing and local standalone dev."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from app.queue.base import AbstractQueue


class MemoryQueue(AbstractQueue):
    """Asynchronous in-memory queue using asyncio.Queue."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def enqueue(self, run_id: uuid.UUID, metadata: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {
            "run_id": str(run_id),
            "attempt": 1,
            "created_at": datetime.now(UTC).isoformat(),
        }
        if metadata:
            payload.update(metadata)
        await self._queue.put(payload)

    async def dequeue(self, timeout_seconds: float = 1.0) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout_seconds)
        except TimeoutError:
            return None

    async def qsize(self) -> int:
        return self._queue.qsize()
