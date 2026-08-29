"""Async Worker Loop — dequeues jobs and dispatches to ExecutionService.

Architecture §4, §7, Rules §3, §7:
- Bounded concurrency via asyncio.Semaphore.
- Graceful shutdown via asyncio.Event.
- Periodic timed-out job reclamation.
- No unbounded asyncio.gather (Rule §7).
- Entry point: python -m app.worker

Usage:
    python -m app.worker
    python -m app.worker --max-concurrent 10 --poll-interval 2.0
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
from typing import Final

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.db.session import async_session_factory
from app.providers.router import ProviderRouter
from app.queue.base import AbstractQueue
from app.queue.memory_queue import MemoryQueue
from app.services.execution import ExecutionService

logger: logging.Logger = get_logger("worker")

DEFAULT_MAX_CONCURRENT: Final[int] = 5
DEFAULT_POLL_INTERVAL: Final[float] = 1.0
RECLAIM_INTERVAL_SECONDS: Final[float] = 30.0


def _create_queue() -> AbstractQueue:
    """Create the appropriate queue backend."""
    try:
        from app.queue.redis_queue import RedisQueue

        return RedisQueue()
    except Exception:
        logger.warning("Redis unavailable, falling back to in-memory queue")
        return MemoryQueue()


async def _reclaim_loop(
    queue: AbstractQueue,
    shutdown: asyncio.Event,
) -> None:
    """Periodically reclaim stranded in-flight jobs."""
    while not shutdown.is_set():
        try:
            reclaimed = await queue.reclaim_timed_out_jobs()
            if reclaimed > 0:
                logger.info(
                    "Reclaimed timed-out jobs",
                    extra={"extra_fields": {"count": reclaimed}},
                )
        except Exception as exc:
            logger.error(
                "Reclaim loop error",
                extra={"extra_fields": {"error": str(exc)}},
            )

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=RECLAIM_INTERVAL_SECONDS)
            break
        except TimeoutError:
            continue


async def run_worker(
    queue: AbstractQueue | None = None,
    router: ProviderRouter | None = None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Main worker loop: poll queue → execute runs with bounded concurrency."""
    settings = get_settings()
    setup_logging(level=settings.log_level)

    queue = queue or _create_queue()
    router = router or ProviderRouter()
    shutdown = shutdown_event or asyncio.Event()
    semaphore = asyncio.Semaphore(max_concurrent)

    execution_service = ExecutionService(
        session_factory=async_session_factory,
        queue=queue,
        router=router,
    )

    # Register signal handlers for graceful shutdown
    if shutdown_event is None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, shutdown.set)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler for SIGTERM
                signal.signal(sig, lambda s, f: shutdown.set())

    logger.info(
        "Worker started",
        extra={
            "extra_fields": {
                "max_concurrent": max_concurrent,
                "poll_interval": poll_interval,
            }
        },
    )

    # Start reclaim loop in background
    reclaim_task = asyncio.create_task(_reclaim_loop(queue, shutdown))
    active_tasks: set[asyncio.Task] = set()

    try:
        while not shutdown.is_set():
            # Wait for a concurrency slot
            await semaphore.acquire()

            if shutdown.is_set():
                semaphore.release()
                break

            # Poll for a job
            job = await queue.dequeue(
                consumer_id="worker-main",
                timeout_seconds=poll_interval,
            )

            if job is None:
                semaphore.release()
                continue

            # Dispatch execution as a bounded concurrent task
            async def _run_job(j=job):
                try:
                    await execution_service.execute_run(j)
                except Exception as exc:
                    logger.error(
                        "Unhandled execution error",
                        extra={
                            "extra_fields": {
                                "job_id": j.job_id,
                                "run_id": str(j.run_id),
                                "error": str(exc),
                            }
                        },
                    )
                finally:
                    semaphore.release()

            task = asyncio.create_task(_run_job())
            active_tasks.add(task)
            task.add_done_callback(active_tasks.discard)

    finally:
        # Graceful drain: wait for active tasks to complete
        if active_tasks:
            logger.info(
                "Draining active tasks",
                extra={"extra_fields": {"count": len(active_tasks)}},
            )
            await asyncio.gather(*active_tasks, return_exceptions=True)

        shutdown.set()
        reclaim_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reclaim_task

        await router.close_all()
        logger.info("Worker shutdown complete")


def main() -> None:
    """CLI entry point for the worker."""
    parser = argparse.ArgumentParser(description="LLM Sandbox Worker")
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=DEFAULT_MAX_CONCURRENT,
        help=f"Maximum concurrent executions (default: {DEFAULT_MAX_CONCURRENT})",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Queue poll interval in seconds (default: {DEFAULT_POLL_INTERVAL})",
    )
    args = parser.parse_args()

    asyncio.run(
        run_worker(
            max_concurrent=args.max_concurrent,
            poll_interval=args.poll_interval,
        )
    )


if __name__ == "__main__":
    main()
