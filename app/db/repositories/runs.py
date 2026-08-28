"""Run repository — database access for runs, worker claims, and keyset pagination.

Rules §5, Database Structure §9, §10:
- Single-query hot status lookup with LEFT JOIN run_results.
- Single-query worker context retrieval.
- Keyset pagination for user run histories.
- Conditional state transitions (QUEUED -> RUNNING -> COMPLETED).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models.challenge_model_binding import ChallengeModelBinding
from app.db.models.challenge_version import ChallengeVersion
from app.db.models.model import Model
from app.db.models.provider import Provider
from app.db.models.run import Run
from app.db.models.run_result import RunResult

TerminalRunStatus = Literal[
    "COMPLETED",
    "PROVIDER_ERROR",
    "TIMEOUT",
    "RATE_LIMITED",
    "VALIDATION_ERROR",
    "ADMISSION_REJECTED",
    "SYSTEM_ERROR",
]
VALID_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "COMPLETED",
    "PROVIDER_ERROR",
    "TIMEOUT",
    "RATE_LIMITED",
    "VALIDATION_ERROR",
    "ADMISSION_REJECTED",
    "SYSTEM_ERROR",
})


@dataclass(frozen=True)
class WorkerRunContext:
    """Full execution context resolved for worker in 1 query (Database Structure §10)."""

    run_id: uuid.UUID
    user_id: uuid.UUID
    prompt_hash: str
    prompt_bytes: int
    attempt_count: int
    system_prompt_ciphertext: str
    max_input_tokens: int
    max_output_tokens: int
    temperature: Decimal
    timeout_ms: int
    model_name: str
    provider_code: str
    provider_kind: str


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_run(
        self,
        user_id: uuid.UUID,
        model_binding_id: int,
        prompt_hash: str,
        prompt_bytes: int,
    ) -> Run:
        """Create a new run in QUEUED state."""
        run = Run(
            user_id=user_id,
            model_binding_id=model_binding_id,
            status="QUEUED",
            prompt_hash=prompt_hash,
            prompt_bytes=prompt_bytes,
            attempt_count=0,
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def get_run_with_result(
        self,
        run_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
    ) -> Run | None:
        """Hot status query: 1 primary-key lookup + 1-to-1 join (Database Structure §10)."""
        stmt = (
            select(Run)
            .where(Run.id == run_id)
            .options(selectinload(Run.result))
        )
        if user_id is not None:
            stmt = stmt.where(Run.user_id == user_id)

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_worker_context(self, run_id: uuid.UUID) -> WorkerRunContext | None:
        """Fetch all execution context for a run in exactly 1 bounded query join."""
        stmt = (
            select(
                Run.id,
                Run.user_id,
                Run.prompt_hash,
                Run.prompt_bytes,
                Run.attempt_count,
                ChallengeVersion.system_prompt_ciphertext,
                ChallengeModelBinding.max_input_tokens,
                ChallengeModelBinding.max_output_tokens,
                ChallengeModelBinding.temperature,
                ChallengeModelBinding.timeout_ms,
                Model.model_name,
                Provider.code,
                Provider.kind,
            )
            .join(
                ChallengeModelBinding,
                ChallengeModelBinding.id == Run.model_binding_id,
            )
            .join(
                ChallengeVersion,
                ChallengeVersion.id == ChallengeModelBinding.challenge_version_id,
            )
            .join(Model, Model.id == ChallengeModelBinding.model_id)
            .join(Provider, Provider.id == Model.provider_id)
            .where(Run.id == run_id)
        )
        result = await self.session.execute(stmt)
        row = result.first()
        if not row:
            return None

        return WorkerRunContext(
            run_id=row[0],
            user_id=row[1],
            prompt_hash=row[2],
            prompt_bytes=row[3],
            attempt_count=row[4],
            system_prompt_ciphertext=row[5],
            max_input_tokens=row[6],
            max_output_tokens=row[7],
            temperature=row[8],
            timeout_ms=row[9],
            model_name=row[10],
            provider_code=row[11],
            provider_kind=row[12],
        )

    async def claim_run(self, run_id: uuid.UUID) -> bool:
        """Atomically transition run state from QUEUED -> RUNNING.

        Returns True if claimed, False if already claimed or in another state.
        """
        now = datetime.now(UTC)
        stmt = (
            update(Run)
            .where(Run.id == run_id, Run.status == "QUEUED")
            .values(
                status="RUNNING",
                started_at=now,
                attempt_count=Run.attempt_count + 1,
            )
        )
        result = await self.session.execute(stmt)
        return result.rowcount > 0

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
        if status not in VALID_TERMINAL_STATUSES:
            raise ValueError(f"Invalid terminal run status: {status}")

        now = datetime.now(UTC)
        stmt = (
            update(Run)
            .where(Run.id == run_id, Run.status == "RUNNING")
            .values(
                status=status,
                finished_at=now,
            )
        )
        result = await self.session.execute(stmt)
        if result.rowcount == 0:
            return False

        # Upsert or insert RunResult
        run_result = RunResult(
            run_id=run_id,
            response_object_key=response_object_key,
            response_preview=response_preview,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            finish_reason=finish_reason,
            provider_request_id=provider_request_id,
            created_at=now,
        )
        await self.session.merge(run_result)
        return True

    async def get_user_history(
        self,
        user_id: uuid.UUID,
        cursor_created_at: datetime | None = None,
        cursor_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[Run]:
        """Fetch user run history using keyset pagination (Database Structure §9).

        Avoids deep OFFSET pagination by leveraging idx_runs_user_created_id.
        """
        stmt = (
            select(Run)
            .where(Run.user_id == user_id)
            .options(selectinload(Run.result))
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(min(limit, 100))
        )

        if cursor_created_at is not None and cursor_id is not None:
            stmt = stmt.where(
                or_(
                    Run.created_at < cursor_created_at,
                    and_(
                        Run.created_at == cursor_created_at,
                        Run.id < cursor_id,
                    ),
                )
            )

        result = await self.session.execute(stmt)
        return list(result.scalars().all())
