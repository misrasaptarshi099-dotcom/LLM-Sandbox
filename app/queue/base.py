"""Abstract Queue interface and QueueJob model.

Rules §2, §3, Implementation Plan Phase 4:
- Minimal sanitized job payload: only run_id and attempt count.
- Zero prompts, system prompts, or credentials stored in queue state.
- Reliable delivery: explicit ACK, NACK with dead-letter queue, visibility timeout.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

MAX_ATTEMPTS: int = 3
DEFAULT_VISIBILITY_TIMEOUT_SECONDS: float = 60.0


@dataclass
class QueueJob:
    """Sanitized queue job model."""

    job_id: str
    run_id: uuid.UUID
    attempt: int = 1
    enqueued_at: float = field(default_factory=time.time)
    delivery_token: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict[str, str | int | float]:
        return {
            "job_id": self.job_id,
            "run_id": str(self.run_id),
            "attempt": self.attempt,
            "enqueued_at": self.enqueued_at,
            "delivery_token": self.delivery_token,
        }

    @classmethod
    def from_dict(cls, data: dict) -> QueueJob:
        return cls(
            job_id=str(data["job_id"]),
            run_id=uuid.UUID(str(data["run_id"])),
            attempt=int(data.get("attempt", 1)),
            enqueued_at=float(data.get("enqueued_at", time.time())),
            delivery_token=str(data.get("delivery_token", "")),
        )


class AbstractQueue(ABC):
    """Base interface for reliable job queues."""

    @abstractmethod
    async def enqueue(self, run_id: uuid.UUID, attempt: int = 1) -> str:
        """Enqueue a sanitized run job. Returns the generated job_id."""
        ...

    @abstractmethod
    async def dequeue(
        self,
        consumer_id: str = "worker-1",
        timeout_seconds: float = 1.0,
        visibility_timeout_seconds: float = DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    ) -> QueueJob | None:
        """Atomically dequeue a job and move to in-flight state."""
        ...

    @abstractmethod
    async def ack(self, job_id: str, delivery_token: str | None = None) -> bool:
        """Acknowledge successful job processing and remove from in-flight."""
        ...

    @abstractmethod
    async def nack(
        self, job_id: str, delivery_token: str | None = None, requeue: bool = True
    ) -> bool:
        """Negative acknowledge.

        If requeue is True and attempt < MAX_ATTEMPTS, requeues for retry.
        Otherwise moves job to Dead-Letter Queue (DLQ).
        """
        ...

    @abstractmethod
    async def reclaim_timed_out_jobs(
        self,
        visibility_timeout_seconds: float = DEFAULT_VISIBILITY_TIMEOUT_SECONDS,
    ) -> int:
        """Reclaim stranded in-flight jobs that exceeded visibility timeout."""
        ...

    @abstractmethod
    async def get_metrics(self) -> dict[str, int]:
        """Return approximate queue depth metrics: queued, in_flight, dead_letter."""
        ...

    @abstractmethod
    async def qsize(self) -> int:
        """Return number of waiting jobs in queue."""
        ...
