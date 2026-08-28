"""Unit tests for Reliable MemoryQueue and RedisQueue implementations."""

from __future__ import annotations

import asyncio
import uuid

import fakeredis.aioredis
import pytest

from app.queue.memory_queue import MemoryQueue
from app.queue.redis_queue import RedisQueue


@pytest.mark.asyncio
async def test_memory_queue_basic_ack_lifecycle() -> None:
    queue = MemoryQueue()
    run_id = uuid.uuid4()

    job_id = await queue.enqueue(run_id)
    assert job_id.startswith("job_")
    assert await queue.qsize() == 1

    # Dequeue
    job = await queue.dequeue(timeout_seconds=0.1)
    assert job is not None
    assert job.job_id == job_id
    assert job.run_id == run_id
    assert job.attempt == 1
    assert await queue.qsize() == 0

    # In flight metrics
    metrics = await queue.get_metrics()
    assert metrics["queued"] == 0
    assert metrics["in_flight"] == 1

    # Ack
    acked = await queue.ack(job_id)
    assert acked is True

    # Post-ack metrics
    metrics_after = await queue.get_metrics()
    assert metrics_after["in_flight"] == 0
    assert metrics_after["queued"] == 0


@pytest.mark.asyncio
async def test_memory_queue_nack_and_retry() -> None:
    queue = MemoryQueue(max_attempts=3)
    run_id = uuid.uuid4()

    job_id = await queue.enqueue(run_id)
    job = await queue.dequeue(timeout_seconds=0.1)
    assert job is not None
    assert job.attempt == 1

    # Nack with requeue
    nacked = await queue.nack(job_id, requeue=True)
    assert nacked is True

    # Re-dequeue and verify attempt count incremented
    job_retry = await queue.dequeue(timeout_seconds=0.1)
    assert job_retry is not None
    assert job_retry.job_id == job_id
    assert job_retry.attempt == 2


@pytest.mark.asyncio
async def test_memory_queue_dead_letter_on_max_attempts() -> None:
    queue = MemoryQueue(max_attempts=2)
    run_id = uuid.uuid4()

    job_id = await queue.enqueue(run_id)

    # 1st attempt
    j1 = await queue.dequeue(timeout_seconds=0.1)
    assert j1 is not None
    await queue.nack(job_id, requeue=True)

    # 2nd attempt
    j2 = await queue.dequeue(timeout_seconds=0.1)
    assert j2 is not None
    assert j2.attempt == 2

    # Nack on 2nd attempt -> should route to DLQ
    await queue.nack(job_id, requeue=True)

    metrics = await queue.get_metrics()
    assert metrics["queued"] == 0
    assert metrics["in_flight"] == 0
    assert metrics["dead_letter"] == 1


@pytest.mark.asyncio
async def test_memory_queue_visibility_timeout_reclaim() -> None:
    queue = MemoryQueue(max_attempts=3)
    run_id = uuid.uuid4()

    job_id = await queue.enqueue(run_id)

    # Dequeue with 0.05s visibility timeout
    job = await queue.dequeue(timeout_seconds=0.1, visibility_timeout_seconds=0.05)
    assert job is not None

    # Wait for visibility timeout to elapse
    await asyncio.sleep(0.06)

    reclaimed = await queue.reclaim_timed_out_jobs(visibility_timeout_seconds=0.05)
    assert reclaimed == 1

    # Should be back in waiting queue with attempt 2
    reclaimed_job = await queue.dequeue(timeout_seconds=0.1)
    assert reclaimed_job is not None
    assert reclaimed_job.job_id == job_id
    assert reclaimed_job.attempt == 2


@pytest.mark.asyncio
async def test_redis_queue_with_fakeredis() -> None:
    fake_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisQueue(redis_client=fake_client, prefix="test:queue", max_attempts=3)
    run_id = uuid.uuid4()

    job_id = await queue.enqueue(run_id)
    assert await queue.qsize() == 1

    # Dequeue
    job = await queue.dequeue(timeout_seconds=0.1, visibility_timeout_seconds=0.1)
    assert job is not None
    assert job.job_id == job_id
    assert job.run_id == run_id
    assert job.attempt == 1

    # Metrics
    metrics = await queue.get_metrics()
    assert metrics["queued"] == 0
    assert metrics["in_flight"] == 1

    # Ack
    acked = await queue.ack(job_id)
    assert acked is True

    metrics_after = await queue.get_metrics()
    assert metrics_after["in_flight"] == 0
    assert metrics_after["queued"] == 0

    await fake_client.aclose()
