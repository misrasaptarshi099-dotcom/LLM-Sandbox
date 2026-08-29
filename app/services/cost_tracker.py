"""Event-wide token budget tracker with atomic reservations and outage recovery.

Architecture §13, Rules §3, Implementation Plan Phase 7:
- Tracks cumulative input and output token spend across the entire event.
- Supports atomic pre-admission token reservations to prevent concurrent overspend.
- Reconciles or releases reservations after execution.
- Redis backend with Lua scripts for atomic check-and-reserve.
- In-memory ledger during Redis outages; pending usage is synced into Redis upon recovery.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.logging import get_logger

_BUDGET_INPUT_KEY = "budget:input_tokens"
_BUDGET_OUTPUT_KEY = "budget:output_tokens"
_BUDGET_RESERVED_INPUT_KEY = "budget:reserved_input_tokens"
_BUDGET_RESERVED_OUTPUT_KEY = "budget:reserved_output_tokens"

_LUA_RESERVE_BUDGET_SCRIPT = """
local input_key = KEYS[1]
local output_key = KEYS[2]
local res_input_key = KEYS[3]
local res_output_key = KEYS[4]

local max_input_limit = tonumber(ARGV[1])
local max_output_limit = tonumber(ARGV[2])
local req_input = tonumber(ARGV[3])
local req_output = tonumber(ARGV[4])

local cur_in = tonumber(redis.call('GET', input_key) or '0')
local cur_res_in = tonumber(redis.call('GET', res_input_key) or '0')
local cur_out = tonumber(redis.call('GET', output_key) or '0')
local cur_res_out = tonumber(redis.call('GET', res_output_key) or '0')

if (cur_in + cur_res_in + req_input) > max_input_limit then
    return {0, 'INPUT_EXHAUSTED', cur_in + cur_res_in}
end

if (cur_out + cur_res_out + req_output) > max_output_limit then
    return {0, 'OUTPUT_EXHAUSTED', cur_out + cur_res_out}
end

redis.call('INCRBY', res_input_key, req_input)
redis.call('INCRBY', res_output_key, req_output)

return {1, 'OK'}
"""

_LUA_RECONCILE_BUDGET_SCRIPT = """
local input_key = KEYS[1]
local output_key = KEYS[2]
local res_input_key = KEYS[3]
local res_output_key = KEYS[4]

local rel_input = tonumber(ARGV[1])
local rel_output = tonumber(ARGV[2])
local act_input = tonumber(ARGV[3])
local act_output = tonumber(ARGV[4])

local cur_res_in = tonumber(redis.call('GET', res_input_key) or '0')
local cur_res_out = tonumber(redis.call('GET', res_output_key) or '0')

local new_res_in = math.max(0, cur_res_in - rel_input)
local new_res_out = math.max(0, cur_res_out - rel_output)
redis.call('SET', res_input_key, new_res_in)
redis.call('SET', res_output_key, new_res_out)

if act_input > 0 then
    redis.call('INCRBY', input_key, act_input)
end
if act_output > 0 then
    redis.call('INCRBY', output_key, act_output)
end

return {1, 'OK'}
"""


class CostTracker:
    """Event-wide token budget enforcement and accounting (Architecture §13)."""

    def __init__(self, redis_client: aioredis.Redis | None = None) -> None:
        self.redis = redis_client
        self._mem_input_tokens: int = 0
        self._mem_output_tokens: int = 0
        self._mem_reserved_input: int = 0
        self._mem_reserved_output: int = 0
        # Pending ledger to merge into Redis once recovered
        self._pending_input_sync: int = 0
        self._pending_output_sync: int = 0

    async def _sync_pending_to_redis(self) -> None:
        """Sync any outage in-memory ledger increments into Redis."""
        if self.redis is None:
            return
        if self._pending_input_sync == 0 and self._pending_output_sync == 0:
            return

        sync_in = self._pending_input_sync
        sync_out = self._pending_output_sync
        try:
            pipe = self.redis.pipeline()
            if sync_in > 0:
                pipe.incrby(_BUDGET_INPUT_KEY, sync_in)
            if sync_out > 0:
                pipe.incrby(_BUDGET_OUTPUT_KEY, sync_out)
            await pipe.execute()
            self._pending_input_sync -= sync_in
            self._pending_output_sync -= sync_out
        except Exception as exc:
            # Redis still unreachable, maintain pending counts
            logger = get_logger("cost_tracker")
            logger.debug(
                "Redis still unreachable for sync",
                extra={"extra_fields": {"error": str(exc)}},
            )

    async def reserve_tokens(
        self,
        max_input_tokens: int,
        max_output_tokens: int,
    ) -> tuple[bool, str | None]:
        """Atomically reserve tokens for an admitted run.

        Returns (allowed: bool, error_message: str | None).
        """
        settings = get_settings()
        max_input = settings.event_max_input_tokens
        max_output = settings.event_max_output_tokens

        await self._sync_pending_to_redis()

        if self.redis is not None:
            try:
                res = await self.redis.eval(
                    _LUA_RESERVE_BUDGET_SCRIPT,
                    4,
                    _BUDGET_INPUT_KEY,
                    _BUDGET_OUTPUT_KEY,
                    _BUDGET_RESERVED_INPUT_KEY,
                    _BUDGET_RESERVED_OUTPUT_KEY,
                    str(max_input),
                    str(max_output),
                    str(max_input_tokens),
                    str(max_output_tokens),
                )
                if res[0] == 1:
                    return True, None
                reason = res[1]
                committed = res[2] if len(res) > 2 else 0
                if reason == "INPUT_EXHAUSTED":
                    return False, (
                        "Event input token budget exhausted. "
                        f"Committed {committed:,} + requested {max_input_tokens:,} "
                        f"exceeds limit {max_input:,}."
                    )
                return False, (
                    "Event output token budget exhausted. "
                    f"Committed {committed:,} + requested {max_output_tokens:,} "
                    f"exceeds limit {max_output:,}."
                )
            except Exception as exc:
                logger = get_logger("cost_tracker")
                logger.warning(
                    "Redis unavailable for budget reservation, falling back to memory",
                    extra={"extra_fields": {"error": str(exc)}},
                )

        # In-memory fallback
        total_in = self._mem_input_tokens + self._mem_reserved_input + max_input_tokens
        total_out = self._mem_output_tokens + self._mem_reserved_output + max_output_tokens

        if total_in > max_input:
            committed_in = self._mem_input_tokens + self._mem_reserved_input
            return False, (
                "Event input token budget exhausted. "
                f"Committed {committed_in:,} + requested {max_input_tokens:,} "
                f"exceeds limit {max_input:,}."
            )
        if total_out > max_output:
            committed_out = self._mem_output_tokens + self._mem_reserved_output
            return False, (
                "Event output token budget exhausted. "
                f"Committed {committed_out:,} + requested {max_output_tokens:,} "
                f"exceeds limit {max_output:,}."
            )

        self._mem_reserved_input += max_input_tokens
        self._mem_reserved_output += max_output_tokens
        return True, None

    async def release_reservation(
        self,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
    ) -> None:
        """Release a reservation without committing usage (e.g. run failed/rejected)."""
        await self.reconcile_reservation(
            reserved_input_tokens=reserved_input_tokens,
            reserved_output_tokens=reserved_output_tokens,
            actual_input_tokens=0,
            actual_output_tokens=0,
        )

    async def reconcile_reservation(
        self,
        reserved_input_tokens: int,
        reserved_output_tokens: int,
        actual_input_tokens: int,
        actual_output_tokens: int,
    ) -> None:
        """Reconcile reservation against actual usage recorded post-execution."""
        await self._sync_pending_to_redis()

        if self.redis is not None:
            try:
                await self.redis.eval(
                    _LUA_RECONCILE_BUDGET_SCRIPT,
                    4,
                    _BUDGET_INPUT_KEY,
                    _BUDGET_OUTPUT_KEY,
                    _BUDGET_RESERVED_INPUT_KEY,
                    _BUDGET_RESERVED_OUTPUT_KEY,
                    str(reserved_input_tokens),
                    str(reserved_output_tokens),
                    str(actual_input_tokens),
                    str(actual_output_tokens),
                )
                return
            except Exception as exc:
                logger = get_logger("cost_tracker")
                logger.warning(
                    "Redis unavailable for budget reconciliation, recording to memory",
                    extra={"extra_fields": {"error": str(exc)}},
                )

        # In-memory fallback
        self._mem_reserved_input = max(0, self._mem_reserved_input - reserved_input_tokens)
        self._mem_reserved_output = max(0, self._mem_reserved_output - reserved_output_tokens)
        self._mem_input_tokens += actual_input_tokens
        self._mem_output_tokens += actual_output_tokens
        self._pending_input_sync += actual_input_tokens
        self._pending_output_sync += actual_output_tokens

    async def check_budget(self) -> tuple[bool, str | None]:
        """Check if event-wide token budget is still available."""
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
        """Directly record token usage (unreserved) into counters."""
        if input_tokens < 0 or output_tokens < 0:
            return

        await self._sync_pending_to_redis()

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

        # In-memory fallback: record to memory ledger and mark pending for sync
        self._mem_input_tokens += input_tokens
        self._mem_output_tokens += output_tokens
        self._pending_input_sync += input_tokens
        self._pending_output_sync += output_tokens

    async def get_usage_summary(self) -> dict[str, int]:
        """Return current cumulative token usage (merging Redis and pending ledger)."""
        await self._sync_pending_to_redis()

        if self.redis is not None:
            try:
                pipe = self.redis.pipeline()
                pipe.get(_BUDGET_INPUT_KEY)
                pipe.get(_BUDGET_OUTPUT_KEY)
                results = await pipe.execute()
                redis_in = int(results[0] or 0)
                redis_out = int(results[1] or 0)
                return {
                    "input_tokens": redis_in + self._pending_input_sync,
                    "output_tokens": redis_out + self._pending_output_sync,
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
