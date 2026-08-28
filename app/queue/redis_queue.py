"""Production reliable Redis queue implementation with ACK, NACK, DLQ, and visibility timeouts.

Rules §2, §3, Implementation Plan Phase 4:
- Sanitized job payloads only (run_id + attempt).
- Atomic transitions via Redis pipelines.
- Visibility timeout and stranded job reclamation.
- Dead-Letter-Queue routing after MAX_ATTEMPTS.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.queue.base import (
    DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    AbstractQueue,
    QueueJob,
)


class RedisQueue(AbstractQueue):
    """Production Redis reliable queue."""

    def __init__(
        self,
        redis_client: aioredis.Redis | None = None,
        prefix: str = "llm_sandbox:queue",
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.prefix = prefix
        self.max_attempts = max_attempts
        self.jobs_key = f"{prefix}:jobs"
        self.waiting_key = f"{prefix}:waiting"
        self.in_flight_key = f"{prefix}:in_flight"
        self.dlq_key = f"{prefix}:dlq"

        self._client = redis_client or aioredis.from_url(
            get_settings().redis_url,
            decode_responses=True,
        )

    async def enqueue(self, run_id: uuid.UUID, attempt: int = 1) -> str:
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        job = QueueJob(
            job_id=job_id,
            run_id=run_id,
            attempt=attempt,
            enqueued_at=time.time(),
        )
        payload = json.dumps(job.to_dict())

        pipe = self._client.pipeline()
        pipe.hset(self.jobs_key, job_id, payload)
        pipe.rpush(self.waiting_key, job_id)
        await pipe.execute()
        return job_id

    async def dequeue(
        self,
        consumer_id: str = "worker-1",
        timeout_seconds: float = 1.0,
        visibility_timeout_seconds: float = DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    ) -> QueueJob | None:
        deadline = time.time() + timeout_seconds
        while time.time() <= deadline:
            job_id = await self._client.lpop(self.waiting_key)
            if job_id:
                now = time.time()
                expires_at = now + visibility_timeout_seconds
                pipe = self._client.pipeline()
                pipe.zadd(self.in_flight_key, {job_id: expires_at})
                pipe.hget(self.jobs_key, job_id)
                _, raw = await pipe.execute()

                if not raw:
                    await self._client.zrem(self.in_flight_key, job_id)
                    continue

                data = json.loads(raw)
                return QueueJob.from_dict(data)

            await asyncio.sleep(0.05)
        return None

    async def ack(self, job_id: str) -> bool:
        pipe = self._client.pipeline()
        pipe.zrem(self.in_flight_key, job_id)
        pipe.hdel(self.jobs_key, job_id)
        removed, _ = await pipe.execute()
        return bool(removed and removed > 0)

    async def nack(self, job_id: str, requeue: bool = True) -> bool:
        raw = await self._client.hget(self.jobs_key, job_id)
        if not raw:
            await self._client.zrem(self.in_flight_key, job_id)
            return False

        job_data = json.loads(raw)
        attempt = job_data.get("attempt", 1)

        pipe = self._client.pipeline()
        pipe.zrem(self.in_flight_key, job_id)
        if requeue and attempt < self.max_attempts:
            job_data["attempt"] = attempt + 1
            pipe.hset(self.jobs_key, job_id, json.dumps(job_data))
            pipe.lpush(self.waiting_key, job_id)
        else:
            pipe.hdel(self.jobs_key, job_id)
            pipe.rpush(self.dlq_key, raw)

        await pipe.execute()
        return True

    async def reclaim_timed_out_jobs(
        self,
        visibility_timeout_seconds: float = DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    ) -> int:
        now = time.time()
        expired_ids = await self._client.zrangebyscore(self.in_flight_key, 0, now)
        if not expired_ids:
            return 0

        count = 0
        for jid in expired_ids:
            raw = await self._client.hget(self.jobs_key, jid)
            pipe = self._client.pipeline()
            pipe.zrem(self.in_flight_key, jid)
            if raw:
                job_data = json.loads(raw)
                attempt = job_data.get("attempt", 1)
                if attempt < self.max_attempts:
                    job_data["attempt"] = attempt + 1
                    pipe.hset(self.jobs_key, jid, json.dumps(job_data))
                    pipe.lpush(self.waiting_key, jid)
                    count += 1
                else:
                    pipe.hdel(self.jobs_key, jid)
                    pipe.rpush(self.dlq_key, raw)
            await pipe.execute()

        return count

    async def get_metrics(self) -> dict[str, int]:
        pipe = self._client.pipeline()
        pipe.llen(self.waiting_key)
        pipe.zcard(self.in_flight_key)
        pipe.llen(self.dlq_key)
        queued, in_flight, dlq = await pipe.execute()
        return {
            "queued": queued,
            "in_flight": in_flight,
            "dead_letter": dlq,
        }

    async def qsize(self) -> int:
        return await self._client.llen(self.waiting_key)

    async def close(self) -> None:
        await self._client.aclose()
