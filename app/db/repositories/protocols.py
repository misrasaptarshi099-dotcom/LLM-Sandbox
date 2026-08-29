"""Repository interfaces and structural protocols.

Rules §5, Architecture §10, PRD §4:
- Decouples HTTP routes and orchestration services from raw SQLAlchemy sessions.
- Enforces single-query hot lookups and bounded pagination interfaces.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.db.models.challenge import Challenge
from app.db.models.run import Run
from app.db.repositories.challenges import ChallengeExecutionContext
from app.db.repositories.runs import TerminalRunStatus, WorkerRunContext


@runtime_checkable
class RunRepositoryProtocol(Protocol):
    """Structural protocol for run lifecycle persistence operations."""

    async def create_run(
        self,
        user_id: uuid.UUID,
        model_binding_id: int,
        prompt_hash: str,
        prompt_bytes: int,
        prompt_ciphertext: str,
    ) -> Run:
        """Create a new run in QUEUED state."""
        ...

    async def get_run_with_result(
        self,
        run_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
    ) -> Run | None:
        """Hot status query: 1 primary-key lookup + 1-to-1 join (Rule §5)."""
        ...

    async def get_worker_context(self, run_id: uuid.UUID) -> WorkerRunContext | None:
        """Fetch all execution context for a run in exactly 1 bounded query join."""
        ...

    async def claim_next_run(self, run_id: uuid.UUID) -> bool:
        """Atomic claim transition QUEUED -> RUNNING with attempt increment."""
        ...

    async def reset_run_for_retry(self, run_id: uuid.UUID) -> bool:
        """Transition a RUNNING run back to QUEUED for retryable redelivery."""
        ...

    async def finalize_run(
        self,
        run_id: uuid.UUID,
        status: TerminalRunStatus,
        response_preview: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        duration_ms: int | None = None,
        finish_reason: str | None = None,
        provider_request_id: str | None = None,
        response_object_key: str | None = None,
    ) -> bool:
        """Conditionally transition RUNNING -> terminal status and persist RunResult."""
        ...

    async def get_user_history(
        self,
        user_id: uuid.UUID,
        cursor_created_at: datetime | None = None,
        cursor_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[Run]:
        """Fetch user run history using keyset pagination."""
        ...


@runtime_checkable
class ChallengeRepositoryProtocol(Protocol):
    """Structural protocol for challenge and binding resolution queries."""

    async def get_by_slug(self, slug: str) -> Challenge | None:
        """Fetch a challenge by slug."""
        ...

    async def get_latest_live_binding(
        self,
        challenge_slug: str,
        preferred_model_name: str | None = None,
    ) -> ChallengeExecutionContext | None:
        """Resolve the latest published version and active model binding in 1 query."""
        ...

    async def list_public_challenges(self) -> list[dict[str, Any]]:
        """List active public challenge metadata (safe for participants)."""
        ...
