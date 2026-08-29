"""Sliding Window Quota Key Pool for LLM Providers.

Architecture §5, PRD §11, Phase 14:
- Rolling 60-second sliding window per (key, model) pair.
- Default 14 requests/min per key per model (matches Google AI Studio's per-model rate buckets).
- Model-aware cooldown: a 429 on one model does not block other models on the same key.
- Preempts exhausted keys and selects the key with the highest remaining quota.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Sequence


class SlidingWindowKeyPool:
    """Manages a pool of API keys with 60-second sliding window rate limits per model."""

    def __init__(
        self,
        api_keys: Sequence[str],
        max_rpm: int = 14,
        window_seconds: float = 60.0,
        cooldown_seconds: float = 5.0,
    ) -> None:
        self.api_keys: list[str] = [k.strip() for k in api_keys if k and k.strip()]
        if not self.api_keys:
            raise ValueError("SlidingWindowKeyPool requires at least one API key.")

        self.max_rpm = max_rpm
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds

        # (key, model) -> timestamps of requests
        self._history: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        # (key, model) -> cooldown expiration timestamp
        self._cooldown: dict[tuple[str, str], float] = defaultdict(float)
        self._cursor: int = 0
        self._lock = asyncio.Lock()

    @property
    def total_keys(self) -> int:
        return len(self.api_keys)

    def _prune_history(self, now: float, model: str) -> None:
        """Evict timestamps older than the sliding window for a given model."""
        cutoff = now - self.window_seconds
        for key in self.api_keys:
            history = self._history[(key, model)]
            while history and history[0] <= cutoff:
                history.popleft()

    async def acquire_key(
        self,
        model: str = "default",
        wait_if_unavailable: bool = True,
    ) -> str | None:
        """Acquire an available key with remaining quota for the specified model.

        If all keys are saturated or on cooldown:
        - If wait_if_unavailable is True, sleeps until the earliest slot opens.
        - If wait_if_unavailable is False, returns None immediately without blocking.
        """
        while True:
            async with self._lock:
                now = time.monotonic()
                self._prune_history(now, model)

                # Filter available keys (not on cooldown and below max_rpm for this model)
                available = [
                    k
                    for k in self.api_keys
                    if now >= self._cooldown[(k, model)]
                    and len(self._history[(k, model)]) < self.max_rpm
                ]

                if available:
                    # Pick key with fewest requests in current window for this model
                    min_load = min(len(self._history[(k, model)]) for k in available)
                    candidates = [
                        k for k in available if len(self._history[(k, model)]) == min_load
                    ]

                    chosen = candidates[self._cursor % len(candidates)]
                    self._cursor = (self._cursor + 1) % 1_000_000

                    self._history[(chosen, model)].append(now)
                    return chosen

                if not wait_if_unavailable:
                    return None

                # No key available right now; calculate minimum wait time
                wait_candidates: list[float] = []

                for k in self.api_keys:
                    km = (k, model)
                    if self._cooldown[km] > now:
                        wait_candidates.append(self._cooldown[km] - now)

                    hist = self._history[km]
                    if hist and len(hist) >= self.max_rpm:
                        oldest = hist[0]
                        roll_off = self.window_seconds - (now - oldest)
                        if roll_off > 0:
                            wait_candidates.append(roll_off)

                sleep_time = min(wait_candidates) + 0.05 if wait_candidates else 1.0

            # Sleep outside lock to allow other operations
            await asyncio.sleep(max(0.05, sleep_time))

    async def report_rate_limit(
        self,
        key: str,
        model: str = "default",
        cooldown_seconds: float | None = None,
    ) -> None:
        """Put a (key, model) pair into cooldown after an upstream 429."""
        async with self._lock:
            dur = cooldown_seconds if cooldown_seconds is not None else self.cooldown_seconds
            self._cooldown[(key, model)] = time.monotonic() + dur

    async def get_stats(self, model: str = "default") -> list[dict[str, int | float | str | bool]]:
        """Return snapshot stats for observability."""
        async with self._lock:
            now = time.monotonic()
            self._prune_history(now, model)
            stats: list[dict[str, int | float | str | bool]] = []
            for k in self.api_keys:
                masked = f"{k[:6]}...{k[-4:]}" if len(k) > 10 else "***"
                km = (k, model)
                stats.append(
                    {
                        "key_masked": masked,
                        "model": model,
                        "used_in_window": len(self._history[km]),
                        "max_rpm": self.max_rpm,
                        "is_cooling_down": now < self._cooldown[km],
                        "cooldown_remaining_s": max(0.0, self._cooldown[km] - now),
                    }
                )
            return stats
