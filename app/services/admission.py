"""Run Admission Service.

Rules §4, §5, §10, PRD §4, Architecture §5, §9, Implementation Plan Phase 7:
- Validates prompt byte length (max 4KB / 4096 bytes).
- Enforces per-IP, per-user, and global rate limits before touching heavy resources.
- Rejects admission when queue depth exceeds configured threshold (backpressure).
- Rejects admission when event-wide token budget is exhausted.
- Resolves challenge, published version, and model binding in 1 DB query.
- Commits run row in short transaction and enqueues job ID.
- NEVER waits for model generation.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import encrypt_system_prompt, hash_text
from app.db.repositories.challenges import ChallengeRepository
from app.db.repositories.runs import RunRepository
from app.queue.base import AbstractQueue
from app.schemas.runs import RunCreateRequest, RunCreateResponse
from app.services.cost_tracker import CostTracker
from app.services.rate_limiter import RateLimiter

MAX_PROMPT_BYTES: int = 4096


class AdmissionService:
    def __init__(
        self,
        session: AsyncSession,
        queue: AbstractQueue,
        rate_limiter: RateLimiter,
        cost_tracker: CostTracker | None = None,
    ) -> None:
        self.session = session
        self.queue = queue
        self.rate_limiter = rate_limiter
        self.cost_tracker = cost_tracker
        self.challenge_repo = ChallengeRepository(session)
        self.run_repo = RunRepository(session)

    async def admit_run(
        self,
        user_id: uuid.UUID,
        request: RunCreateRequest,
        client_ip: str | None = None,
    ) -> RunCreateResponse:
        """Validate submission, create QUEUED run row, and enqueue execution job.

        Cost-control order (Phase 7):
        1. Reject impossible requests (prompt validation)
        2. Rate-limit (per-IP → per-user → global)
        3. Admission control (queue depth + event budget)
        4. Resolve challenge + create run + enqueue
        """
        # 1. Prompt Length and Byte Validation (reject impossible requests)
        prompt_bytes = len(request.prompt.encode("utf-8"))
        if prompt_bytes == 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Prompt cannot be empty.",
            )
        if prompt_bytes > MAX_PROMPT_BYTES:
            msg = (
                f"Prompt exceeds maximum allowable size of {MAX_PROMPT_BYTES} bytes "
                f"({prompt_bytes} bytes received)."
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=msg,
            )

        # 2a. Per-IP Rate Limit (Phase 7)
        if client_ip:
            ip_allowed, ip_msg = await self.rate_limiter.check_ip_rate_limit(client_ip)
            if not ip_allowed:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=ip_msg or "IP rate limit exceeded. Please try again shortly.",
                )

        # 2b. Per-User + Global Rate Limit
        allowed, error_msg = await self.rate_limiter.check_rate_limit(user_id)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=error_msg or "Rate limit exceeded. Please try again shortly.",
            )

        # 3a. Queue Depth Admission Control (Architecture §9, Phase 7)
        settings = get_settings()
        try:
            queue_size = await self.queue.qsize()
            if queue_size >= settings.max_queue_depth:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        f"System is at capacity. Queue depth ({queue_size}) "
                        f"exceeds threshold ({settings.max_queue_depth}). "
                        "Please try again later."
                    ),
                )
        except HTTPException:
            raise
        except Exception as exc:
            # If queue metrics unavailable, allow admission (fail-open)
            from app.core.logging import get_logger

            get_logger("admission").warning(
                "Queue size check failed, allowing admission",
                extra={"extra_fields": {"error": str(exc)}},
            )

        # 3b. Resolve active challenge version & allowed model binding in 1 query (Rule §5)
        ctx = await self.challenge_repo.get_latest_live_binding(
            challenge_slug=request.challenge_slug,
            preferred_model_name=request.preferred_model,
        )
        if not ctx:
            msg = (
                f"Challenge '{request.challenge_slug}' is not available "
                "or has no active model bindings."
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=msg,
            )

        # 3c. Event-Wide Token Budget Reservation (Architecture §13, Phase 7)
        reserved_input = ctx.binding.max_input_tokens
        reserved_output = ctx.binding.max_output_tokens
        if self.cost_tracker is not None:
            budget_ok, budget_msg = await self.cost_tracker.reserve_tokens(
                max_input_tokens=reserved_input,
                max_output_tokens=reserved_output,
            )
            if not budget_ok:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=budget_msg or "Event token budget exhausted.",
                )

        # 4. Create Run row in database
        prompt_hash = hash_text(request.prompt)
        prompt_ciphertext = encrypt_system_prompt(request.prompt)
        run = await self.run_repo.create_run(
            user_id=user_id,
            model_binding_id=ctx.binding.id,
            prompt_hash=prompt_hash,
            prompt_bytes=prompt_bytes,
            prompt_ciphertext=prompt_ciphertext,
        )

        # 5. Enqueue run job with transactional rollback safety
        try:
            await self.queue.enqueue(
                run_id=run.id,
                attempt=1,
            )
            # Commit only after queue has acknowledged receipt
            await self.session.commit()
        except Exception as exc:
            if self.cost_tracker is not None:
                await self.cost_tracker.release_reservation(
                    reserved_input_tokens=reserved_input,
                    reserved_output_tokens=reserved_output,
                )
            await self.session.rollback()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Job queue is currently unavailable. Please retry your submission.",
            ) from exc

        return RunCreateResponse(
            run_id=run.id,
            status="QUEUED",
            message="Run accepted and queued for asynchronous execution",
        )
