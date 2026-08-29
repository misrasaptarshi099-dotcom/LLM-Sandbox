"""Event-wide token budget tracker.

Architecture §13, Rules §3, Implementation Plan Phase 7:
- Tracks cumulative input and output token spend across the entire event.
- Redis backend: atomic INCRBY on budget counters.
- In-memory fallback for single-process/test deployments.
- Admission checks budget before accepting new runs.
- Execution records actual usage after provider calls.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.logging import get_logger

_BUDGET_INPUT_KEY = "budget:input_tokens"
_BUDGET_OUTPUT_KEY = "budget:output_tokens"


class CostTracker:
    """Event-wide token budget enforcement (Architecture §13)."""

    def __init__(self, redis_client: aioredis.Redis | None = None) -> None:
        self.redis = redis_client
        self._mem_input_tokens: int = 0
        self._mem_output_tokens: int = 0

    async def check_budget(self) -> tuple[bool, str | None]:
        """Check if event-wide token budget is still available.

        Returns (allowed: bool, error_message: str | None).
        """
        settings = get_settings()
        max_input = settings.event_max_input_tokens
        max_output = settings.event_max_output_tokens

        usage = await self.get_usage_summary()
        current_input = usage["input_tokens"]
        current_output = usage["output_tokens"]

        if current_input >= max_input:
            return False, (
                "Event input token budget exhausted. "
                f"Used {current_input:,} of {max_input:,} input tokens."
            )
        if current_output >= max_output:
            return False, (
                "Event output token budget exhausted. "
                f"Used {current_output:,} of {max_output:,} output tokens."
            )
        return True, None

    async def record_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Record token usage from a completed provider call."""
        if input_tokens < 0 or output_tokens < 0:
            return

        if self.redis is not None:
            try:
                pipe = self.redis.pipeline()
                pipe.incrby(_BUDGET_INPUT_KEY, input_tokens)
                pipe.incrby(_BUDGET_OUTPUT_KEY, output_tokens)
                await pipe.execute()
                return
            except Exception as exc:
                logger = get_logger("cost_tracker")
                logger.warning(
                    "Redis cost tracker unavailable, falling back to memory",
                    extra={"extra_fields": {"error": str(exc)}},
                )

        # In-memory fallback
        self._mem_input_tokens += input_tokens
        self._mem_output_tokens += output_tokens

    async def get_usage_summary(self) -> dict[str, int]:
        """Return current cumulative token usage."""
        if self.redis is not None:
            try:
                pipe = self.redis.pipeline()
                pipe.get(_BUDGET_INPUT_KEY)
                pipe.get(_BUDGET_OUTPUT_KEY)
                results = await pipe.execute()
                return {
                    "input_tokens": int(results[0] or 0),
                    "output_tokens": int(results[1] or 0),
                }
            except Exception as exc:
                logger = get_logger("cost_tracker")
                logger.warning(
                    "Redis cost tracker read failed, using memory",
                    extra={"extra_fields": {"error": str(exc)}},
                )

        return {
            "input_tokens": self._mem_input_tokens,
            "output_tokens": self._mem_output_tokens,
        }
