"""Abstract queue interface for run scheduling."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any


class AbstractQueue(ABC):
    """Base interface for job queues."""

    @abstractmethod
    async def enqueue(self, run_id: uuid.UUID, metadata: dict[str, Any] | None = None) -> None:
        """Enqueue a run job."""
        ...

    @abstractmethod
    async def dequeue(self, timeout_seconds: float = 1.0) -> dict[str, Any] | None:
        """Dequeue a single job payload."""
        ...

    @abstractmethod
    async def qsize(self) -> int:
        """Return approximate queue depth."""
        ...
