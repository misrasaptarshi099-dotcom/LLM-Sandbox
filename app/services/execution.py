"""Challenge Execution Service — worker-side orchestration of a single run.

Architecture §4, §7, Rules §2, §3, §5, Implementation Plan Phase 6:
- Two short DB transactions: (1) claim + load context, (2) persist result.
- LLM call happens between them — no open DB session during provider I/O.
- System prompt and participant prompt are decrypted only inside execute_run().
- Bounded internal retries with exponential backoff for retryable errors.
- Circuit breaker checked before each provider attempt.
- Response preview truncated to RESPONSE_PREVIEW_MAX_CHARS.
- Transient plaintext cleared after execution.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.core.security import decrypt_system_prompt
from app.db.repositories.runs import RunRepository, TerminalRunStatus, WorkerRunContext
from app.providers.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderAuthError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.providers.router import CircuitBreaker, ProviderRouter
from app.queue.base import MAX_ATTEMPTS, AbstractQueue, QueueJob
from app.services.cost_tracker import CostTracker

logger: logging.Logger = get_logger("execution")

# --- Constants ---
RESPONSE_PREVIEW_MAX_CHARS: Final[int] = 500
MAX_INTERNAL_RETRIES: Final[int] = 2
BACKOFF_BASE_SECONDS: Final[float] = 1.0
# Heuristic budget: ~4 chars per token (same as TokenUsage.estimate)
CHARS_PER_TOKEN_ESTIMATE: Final[int] = 4


def _map_error_to_status(error: ProviderError) -> TerminalRunStatus:
    """Map provider exception to terminal run status (Phase 6 error table)."""
    if isinstance(error, ProviderTimeoutError):
        return "TIMEOUT"
    if isinstance(error, ProviderRateLimitError):
        return "RATE_LIMITED"
    if isinstance(error, ProviderInvalidRequestError):
        return "VALIDATION_ERROR"
    if isinstance(error, (ProviderAuthError, ProviderUnavailableError)):
        return "PROVIDER_ERROR"
    return "PROVIDER_ERROR"


def _truncate_preview(text: str, max_chars: int = RESPONSE_PREVIEW_MAX_CHARS) -> str:
    """Truncate response text to bounded preview for RunResult storage."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


class ExecutionService:
    """Stateless orchestrator for a single run execution.

    Lifecycle:
        claim → load context → decrypt → budget check → construct →
        invoke (with retry) → persist → ACK → cleanup
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        queue: AbstractQueue,
        router: ProviderRouter,
        cost_tracker: CostTracker | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._queue = queue
        self._router = router
        self._cost_tracker = cost_tracker

    async def execute_run(self, job: QueueJob) -> None:
        """Full worker execution pipeline for a single dequeued job.

        This method never raises — all errors are mapped to terminal run states.
        """
        run_id = job.run_id
        ctx: WorkerRunContext | None = None

        # --- Transaction 1: Claim + Load Context ---
        try:
            ctx = await self._claim_and_load(run_id)
        except Exception as exc:
            logger.error(
                "Failed to claim/load run context",
                extra={"extra_fields": {"run_id": str(run_id), "error": str(exc)}},
            )
            await self._nack_job(job, requeue=True)
            return

        if ctx is None:
            # Run was already claimed or doesn't exist — skip
            logger.info(
                "Run not claimable (already running or missing)",
                extra={"extra_fields": {"run_id": str(run_id)}},
            )
            await self._nack_job(job, requeue=False)
            return

        # --- Decrypt Prompts (in-memory only, Rule §2) ---
        system_prompt: str | None = None
        user_prompt: str | None = None
        try:
            system_prompt = decrypt_system_prompt(ctx.system_prompt_ciphertext)
            user_prompt = decrypt_system_prompt(ctx.prompt_ciphertext)
        except ValueError as exc:
            logger.error(
                "Decryption failure",
                extra={"extra_fields": {"run_id": str(run_id), "error": str(exc)}},
            )
            await self._finalize_with_status(run_id, "SYSTEM_ERROR")
            if self._cost_tracker is not None:
                await self._cost_tracker.release_reservation(
                    ctx.max_input_tokens, ctx.max_output_tokens
                )
            await self._ack_job(job)
            return

        # --- Input Budget Check (Architecture §9) ---
        max_input_chars = ctx.max_input_tokens * CHARS_PER_TOKEN_ESTIMATE
        total_input_chars = len(system_prompt) + len(user_prompt)
        if total_input_chars > max_input_chars:
            logger.warning(
                "Input budget exceeded",
                extra={
                    "extra_fields": {
                        "run_id": str(run_id),
                        "total_chars": total_input_chars,
                        "max_chars": max_input_chars,
                    }
                },
            )
            await self._finalize_with_status(run_id, "VALIDATION_ERROR")
            if self._cost_tracker is not None:
                await self._cost_tracker.release_reservation(
                    ctx.max_input_tokens, ctx.max_output_tokens
                )
            await self._ack_job(job)
            return

        # --- Construct LLMRequest (Architecture §7) ---
        timeout_seconds = ctx.timeout_ms / 1000.0
        llm_request = LLMRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model_name=ctx.model_name,
            temperature=float(ctx.temperature),
            max_tokens=ctx.max_output_tokens,
            timeout_seconds=timeout_seconds,
        )

        # --- Invoke Provider with Bounded Retry + Concurrency Semaphore ---
        provider: LLMProvider = self._router.get_provider(
            provider_kind=ctx.provider_kind,
            provider_code=ctx.provider_code,
        )
        breaker: CircuitBreaker = self._router.get_circuit_breaker(ctx.provider_code)
        semaphore = self._router.get_concurrency_semaphore(ctx.provider_code)
        response: LLMResponse | None = None
        last_error: ProviderError | None = None

        for attempt in range(1 + MAX_INTERNAL_RETRIES):
            # Circuit breaker gate
            if breaker.is_open:
                logger.warning(
                    "Circuit breaker open, skipping provider call",
                    extra={"extra_fields": {"run_id": str(run_id), "attempt": attempt}},
                )
                last_error = ProviderUnavailableError(
                    "Circuit breaker open — provider temporarily unavailable",
                    provider=ctx.provider_code,
                )
                break

            try:
                async with semaphore:
                    response = await provider.generate(llm_request)
                breaker.record_success()
                break
            except ProviderError as exc:
                last_error = exc
                breaker.record_failure(exc)
                if not exc.retryable or attempt >= MAX_INTERNAL_RETRIES:
                    break
                # Exponential backoff before next attempt
                backoff = BACKOFF_BASE_SECONDS * (2**attempt)
                logger.info(
                    "Retrying provider call",
                    extra={
                        "extra_fields": {
                            "run_id": str(run_id),
                            "attempt": attempt + 1,
                            "backoff_s": backoff,
                            "error": str(exc),
                        }
                    },
                )
                await asyncio.sleep(backoff)

        # --- Clear transient plaintext (Rule §2) ---
        system_prompt = None  # noqa: F841
        user_prompt = None  # noqa: F841

        # --- Persist Result + Record Budget ---
        if response is not None:
            await self._finalize_success(run_id, response)
            # Reconcile reservation with actual token usage (Phase 7)
            if self._cost_tracker is not None:
                await self._cost_tracker.reconcile_reservation(
                    reserved_input_tokens=ctx.max_input_tokens,
                    reserved_output_tokens=ctx.max_output_tokens,
                    actual_input_tokens=response.usage.prompt_tokens,
                    actual_output_tokens=response.usage.completion_tokens,
                )
            await self._ack_job(job)
        else:
            # Check if queue-level redelivery is eligible
            is_retryable = last_error is not None and last_error.retryable
            can_requeue = is_retryable and job.attempt < MAX_ATTEMPTS

            if can_requeue:
                # Reset run state to QUEUED so claim_run accepts it upon redelivery
                await self._reset_run_to_queued(run_id)
                await self._nack_job(job, requeue=True)
            else:
                # Terminal finalization: release reservation and ACK job to prevent redelivery
                terminal_status = _map_error_to_status(last_error) if last_error else "SYSTEM_ERROR"
                await self._finalize_with_status(run_id, terminal_status)
                if self._cost_tracker is not None:
                    await self._cost_tracker.release_reservation(
                        reserved_input_tokens=ctx.max_input_tokens,
                        reserved_output_tokens=ctx.max_output_tokens,
                    )
                await self._ack_job(job)

    # --- Internal Helpers ---

    async def _claim_and_load(self, run_id) -> WorkerRunContext | None:
        """Transaction 1: Atomically claim the run and load execution context."""
        async with self._session_factory() as session:
            repo = RunRepository(session)
            claimed = await repo.claim_run(run_id)
            if not claimed:
                await session.commit()
                return None

            ctx = await repo.get_worker_context(run_id)
            await session.commit()
            return ctx

    async def _finalize_success(self, run_id, response: LLMResponse) -> None:
        """Transaction 2: Persist COMPLETED status and RunResult."""
        async with self._session_factory() as session:
            repo = RunRepository(session)
            preview = _truncate_preview(response.text)
            await repo.finalize_run(
                run_id=run_id,
                status="COMPLETED",
                response_preview=preview,
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                duration_ms=int(response.latency_ms),
                finish_reason=response.raw_finish_reason,
            )
            await session.commit()

        logger.info(
            "Run completed",
            extra={
                "extra_fields": {
                    "run_id": str(run_id),
                    "input_tokens": response.usage.prompt_tokens,
                    "output_tokens": response.usage.completion_tokens,
                    "latency_ms": int(response.latency_ms),
                }
            },
        )

    async def _finalize_with_status(self, run_id, status: TerminalRunStatus) -> None:
        """Transaction 2 (error path): Persist terminal error status."""
        async with self._session_factory() as session:
            repo = RunRepository(session)
            await repo.finalize_run(run_id=run_id, status=status)
            await session.commit()

        logger.warning(
            "Run finalized with error status",
            extra={"extra_fields": {"run_id": str(run_id), "status": status}},
        )

    async def _reset_run_to_queued(self, run_id) -> None:
        """Transaction (retry path): Reset RUNNING run back to QUEUED for redelivery."""
        async with self._session_factory() as session:
            repo = RunRepository(session)
            await repo.reset_run_for_retry(run_id)
            await session.commit()

    async def _ack_job(self, job: QueueJob) -> None:
        """Acknowledge job completion to remove from in-flight."""
        try:
            await self._queue.ack(job.job_id, delivery_token=job.delivery_token)
        except Exception as exc:
            logger.error(
                "Failed to ACK job",
                extra={"extra_fields": {"job_id": job.job_id, "error": str(exc)}},
            )

    async def _nack_job(self, job: QueueJob, *, requeue: bool) -> None:
        """Negative-acknowledge job for redelivery or DLQ."""
        try:
            await self._queue.nack(job.job_id, delivery_token=job.delivery_token, requeue=requeue)
        except Exception as exc:
            logger.error(
                "Failed to NACK job",
                extra={"extra_fields": {"job_id": job.job_id, "error": str(exc)}},
            )
