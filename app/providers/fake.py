"""Deterministic Fake LLM Provider for unit tests, CI, and local offline development."""

from __future__ import annotations

import asyncio
import time

from app.providers.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderError,
    TokenUsage,
)


class FakeLLMProvider(LLMProvider):
    """Deterministic mock provider."""

    def __init__(
        self,
        default_response: str = "This is a default response from the Mock LLM.",
        simulated_latency_seconds: float = 0.01,
        exception_to_raise: ProviderError | None = None,
        supported_models: set[str] | None = None,
    ) -> None:
        self.default_response = default_response
        self.simulated_latency_seconds = simulated_latency_seconds
        self.exception_to_raise = exception_to_raise
        self.supported_models = supported_models or {"mock-llm", "mock-gpt-4o", "fake-model"}
        self.calls: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        start_time = time.perf_counter()
        self.calls.append(request)

        if self.simulated_latency_seconds > 0:
            await asyncio.sleep(self.simulated_latency_seconds)

        if self.exception_to_raise is not None:
            raise self.exception_to_raise

        # Check if user prompt requested a specific simulated answer or flag
        response_text = self.default_response
        if "echo:" in request.user_prompt.lower():
            response_text = request.user_prompt

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        usage = TokenUsage.estimate(
            prompt_text=f"{request.system_prompt} {request.user_prompt}",
            completion_text=response_text,
        )

        return LLMResponse(
            text=response_text,
            model_name=request.model_name,
            usage=usage,
            latency_ms=latency_ms,
            raw_finish_reason="stop",
        )

    async def health_check(self) -> bool:
        return self.exception_to_raise is None

    def supports_model(self, model_name: str) -> bool:
        return model_name in self.supported_models

    async def close(self) -> None:
        pass
