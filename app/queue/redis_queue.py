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
        job_id = f"job_{uuid.uuid4().hex}"
        job = QueueJob(
            job_id=job_id,
            run_id=run_id,
            attempt=attempt,
            enqueued_at=time.time(),
            delivery_token=uuid.uuid4().hex,
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
        # Atomic dequeue lua script: pops from waiting_key, gets payload from jobs_key,
        # updates delivery_token in payload, updates jobs_key, and sets in_flight_key deadline.
        lua_dequeue = """
        local job_id = redis.call('LPOP', KEYS[1])
        if not job_id then
            return nil
        end
        local raw = redis.call('HGET', KEYS[2], job_id)
        if not raw then
            return nil
        end
        local job = cjson.decode(raw)
        job['delivery_token'] = ARGV[2]
        local updated_raw = cjson.encode(job)
        redis.call('HSET', KEYS[2], job_id, updated_raw)
        redis.call('ZADD', KEYS[3], ARGV[1], job_id)
        return updated_raw
        """
        deadline = time.time() + timeout_seconds
        while time.time() <= deadline:
            now = time.time()
            expires_at = now + visibility_timeout_seconds
            new_delivery_token = uuid.uuid4().hex
            
            try:
                raw = await self._client.eval(
                    lua_dequeue,
                    3,
                    self.waiting_key,
                    self.jobs_key,
                    self.in_flight_key,
                    str(expires_at),
                    new_delivery_token,
                )
            except Exception:
                # Fallback to pipeline if lua fails in mock/unsupported environments
                job_id = await self._client.lpop(self.waiting_key)
                if job_id:
                    pipe = self._client.pipeline()
                    pipe.zadd(self.in_flight_key, {job_id: expires_at})
                    pipe.hget(self.jobs_key, job_id)
                    _, raw = await pipe.execute()
                    if not raw:
                        await self._client.zrem(self.in_flight_key, job_id)
                        continue
                    data = json.loads(raw)
                    data["delivery_token"] = new_delivery_token
                    await self._client.hset(self.jobs_key, job_id, json.dumps(data))
                    return QueueJob.from_dict(data)
                raw = None

            if raw:
                data = json.loads(raw)
                return QueueJob.from_dict(data)

            await asyncio.sleep(0.05)
        return None

    async def ack(self, job_id: str, delivery_token: str | None = None) -> bool:
        lua_ack = """
        local raw = redis.call('HGET', KEYS[2], ARGV[1])
        if not raw then
            redis.call('ZREM', KEYS[1], ARGV[1])
            return 0
        end
        if ARGV[2] ~= '' then
            local job = cjson.decode(raw)
            if job['delivery_token'] ~= ARGV[2] then
                return 0
            end
        end
        redis.call('ZREM', KEYS[1], ARGV[1])
        redis.call('HDEL', KEYS[2], ARGV[1])
        return 1
        """
        try:
            res = await self._client.eval(
                lua_ack,
                2,
                self.in_flight_key,
                self.jobs_key,
                job_id,
                delivery_token or "",
            )
            return bool(res and int(res) == 1)
        except Exception:
            # Fallback
            raw = await self._client.hget(self.jobs_key, job_id)
            if not raw:
                await self._client.zrem(self.in_flight_key, job_id)
                return False
            if delivery_token:
                data = json.loads(raw)
                if data.get("delivery_token") != delivery_token:
                    return False
            pipe = self._client.pipeline()
            pipe.zrem(self.in_flight_key, job_id)
            pipe.hdel(self.jobs_key, job_id)
            removed, _ = await pipe.execute()
            return bool(removed and removed > 0)

    async def nack(
        self,
        job_id: str,
        delivery_token: str | None = None,
        requeue: bool = True,
    ) -> bool:
        raw = await self._client.hget(self.jobs_key, job_id)
        if not raw:
            await self._client.zrem(self.in_flight_key, job_id)
            return False

        job_data = json.loads(raw)
        if delivery_token is not None and job_data.get("delivery_token") != delivery_token:
            return False

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
