"""In-memory reliable queue implementation for unit tests and local standalone execution."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque

from app.queue.base import (
    DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    MAX_ATTEMPTS,
    AbstractQueue,
    QueueJob,
)


class MemoryQueue(AbstractQueue):
    """Reliable in-memory queue with ACK, NACK, DLQ, and visibility timeout."""

    def __init__(self, max_attempts: int = MAX_ATTEMPTS) -> None:
        self.max_attempts = max_attempts
        self._lock = asyncio.Lock()
        self._waiting: deque[str] = deque()
        self._jobs: dict[str, QueueJob] = {}
        self._in_flight: dict[str, float] = {}  # job_id -> expires_at
        self._dead_letter: list[QueueJob] = []

    async def enqueue(self, run_id: uuid.UUID, attempt: int = 1) -> str:
        async with self._lock:
            job_id = f"job_{uuid.uuid4().hex[:12]}"
            job = QueueJob(
                job_id=job_id,
                run_id=run_id,
                attempt=attempt,
                enqueued_at=time.time(),
            )
            self._jobs[job_id] = job
            self._waiting.append(job_id)
            return job_id

    async def dequeue(
        self,
        consumer_id: str = "worker-1",
        timeout_seconds: float = 1.0,
        visibility_timeout_seconds: float = DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    ) -> QueueJob | None:
        deadline = time.time() + timeout_seconds
        while time.time() <= deadline:
            async with self._lock:
                if self._waiting:
                    job_id = self._waiting.popleft()
                    if job_id in self._jobs:
                        job = self._jobs[job_id]
                        expires_at = time.time() + visibility_timeout_seconds
                        self._in_flight[job_id] = expires_at
                        return job
            await asyncio.sleep(0.02)
        return None

    async def ack(self, job_id: str) -> bool:
        async with self._lock:
            if job_id in self._in_flight:
                del self._in_flight[job_id]
                self._jobs.pop(job_id, None)
                return True
            return False

    async def nack(self, job_id: str, requeue: bool = True) -> bool:
        async with self._lock:
            if job_id not in self._in_flight:
                return False
            del self._in_flight[job_id]

            job = self._jobs.get(job_id)
            if not job:
                return False

            if requeue and job.attempt < self.max_attempts:
                job.attempt += 1
                self._waiting.append(job_id)
            else:
                self._dead_letter.append(job)
                self._jobs.pop(job_id, None)
            return True

    async def reclaim_timed_out_jobs(
        self,
        visibility_timeout_seconds: float = DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    ) -> int:
        now = time.time()
        reclaimed_count = 0
        async with self._lock:
            timed_out_ids = [
                jid for jid, expires_at in self._in_flight.items() if now >= expires_at
            ]
            for jid in timed_out_ids:
                del self._in_flight[jid]
                job = self._jobs.get(jid)
                if job:
                    if job.attempt < self.max_attempts:
                        job.attempt += 1
                        self._waiting.append(jid)
                        reclaimed_count += 1
                    else:
                        self._dead_letter.append(job)
                        self._jobs.pop(jid, None)
        return reclaimed_count

    async def get_metrics(self) -> dict[str, int]:
        async with self._lock:
            return {
                "queued": len(self._waiting),
                "in_flight": len(self._in_flight),
                "dead_letter": len(self._dead_letter),
            }

    async def qsize(self) -> int:
        async with self._lock:
            return len(self._waiting)
