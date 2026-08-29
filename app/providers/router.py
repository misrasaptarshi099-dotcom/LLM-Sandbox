"""Provider Router and Registry with Model Allowlist and Circuit Breakers.

Rules §2, §3, Architecture §5, Implementation Plan Phase 5:
- Factory and router resolving provider kinds: FAKE, GEMINI, OPENAI_COMPATIBLE, SELF_HOSTED.
- Model allowlist validation.
- Circuit breaker state tracking to prevent cascade failures against down providers.
- Safe client lifecycle management.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.core.config import get_settings
from app.providers.base import (
    LLMProvider,
    ProviderAuthError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
)
from app.providers.fake import FakeLLMProvider
from app.providers.gemini import GeminiProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.self_hosted import SelfHostedProvider

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """In-memory circuit breaker for provider fault isolation (Rule §3)."""

    def __init__(
        self,
        failure_threshold: int = 15,
        recovery_timeout_seconds: float = 30.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self._failure_count: int = 0
        self._last_failure_time: float = 0.0
        self._state: str = "CLOSED"  # CLOSED, OPEN, HALF_OPEN

    @property
    def is_open(self) -> bool:
        if self._state == "OPEN":
            if time.time() - self._last_failure_time > self.recovery_timeout_seconds:
                self._state = "HALF_OPEN"
                return False
            return True
        return False

    def record_success(self) -> None:
        self._failure_count = 0
        self._state = "CLOSED"

    def record_failure(self, error: Exception) -> None:
        # Do not trip on deterministic client errors or rate limits
        # (rate limits are handled by key pool & model cascade)
        if isinstance(
            error, (ProviderInvalidRequestError, ProviderAuthError, ProviderRateLimitError)
        ):
            return

        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._failure_count >= self.failure_threshold:
            self._state = "OPEN"
            logger.warning(
                "Circuit breaker tripped OPEN for provider. Failure count: %d",
                self._failure_count,
            )


class ProviderRouter:
    """Registry and factory resolving LLM providers based on DB config and settings."""

    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {}
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        self._concurrency_semaphores: dict[str, asyncio.Semaphore] = {}

    def get_circuit_breaker(self, provider_code: str) -> CircuitBreaker:
        if provider_code not in self._circuit_breakers:
            self._circuit_breakers[provider_code] = CircuitBreaker()
        return self._circuit_breakers[provider_code]

    def get_concurrency_semaphore(self, provider_code: str) -> asyncio.Semaphore:
        """Return a per-provider concurrency semaphore (Phase 7)."""
        if provider_code not in self._concurrency_semaphores:
            max_concurrency = get_settings().provider_max_concurrency
            self._concurrency_semaphores[provider_code] = asyncio.Semaphore(max_concurrency)
        return self._concurrency_semaphores[provider_code]

    def get_provider(
        self,
        provider_kind: str,
        provider_code: str = "default",
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> LLMProvider:
        """Resolve or instantiate provider instance for given kind."""
        key_fingerprint = f"key:{hash(api_key)}" if api_key else "nokey"
        cache_key = f"{provider_kind.upper()}:{provider_code}:{base_url or ''}:{key_fingerprint}"

        if cache_key in self._providers:
            return self._providers[cache_key]

        kind_upper = provider_kind.upper()
        provider_instance: LLMProvider

        if kind_upper == "FAKE":
            provider_instance = FakeLLMProvider()
        elif kind_upper in {"GEMINI", "GEMINI_COMPATIBLE"}:
            provider_instance = GeminiProvider(
                api_key=api_key,
                base_url=base_url,
                timeout_seconds=get_settings().provider_timeout_seconds,
            )
        elif kind_upper == "OPENAI_COMPATIBLE":
            provider_instance = OpenAICompatibleProvider(
                api_key=api_key,
                base_url=base_url,
                provider_name=provider_code,
            )
        elif kind_upper in {"SELF_HOSTED", "OLLAMA", "VLLM"}:
            provider_instance = SelfHostedProvider(
                base_url=base_url,
            )
        else:
            raise ProviderError(
                f"Unsupported provider kind: {provider_kind}",
                provider=provider_code,
            )

        self._providers[cache_key] = provider_instance
        return provider_instance

    async def close_all(self) -> None:
        """Close all managed provider connection pools."""
        for provider in self._providers.values():
            try:
                await provider.close()
            except Exception as exc:
                logger.warning("Error closing provider client: %s", exc)
        self._providers.clear()
