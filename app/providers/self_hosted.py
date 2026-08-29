"""Self-hosted LLM Provider adapter (Ollama, vLLM, LocalAI).

Rules §2, §3, Architecture §5, Implementation Plan Phase 5:
- Uses OpenAI-compatible /v1 endpoints supported natively by vLLM, Ollama, and LocalAI.
- Strict localhost/internal host validation.
- Full error classification and normalized responses.
"""

from __future__ import annotations

import httpx

from app.core.config import get_settings
from app.providers.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    validate_provider_url,
)
from app.providers.openai_compatible import OpenAICompatibleProvider


class SelfHostedProvider(LLMProvider):
    """Self-hosted LLM adapter for Ollama, vLLM, or LocalAI."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout_seconds: float = 60.0,
        allowed_hosts: list[str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        raw_base_url = base_url or settings.ollama_base_url
        self.allowed_hosts = allowed_hosts or settings.provider_allowed_hosts

        # Ensure base_url has /v1 suffix for standard OpenAI-compatible endpoints
        cleaned_url = raw_base_url.rstrip("/")
        if not cleaned_url.endswith("/v1"):
            cleaned_url = f"{cleaned_url}/v1"

        self.base_url = validate_provider_url(
            cleaned_url,
            allowed_hosts=self.allowed_hosts,
            allow_http_localhost=True,
        )

        # Delegate execution to OpenAICompatibleProvider with local credentials
        self._adapter = OpenAICompatibleProvider(
            api_key="ollama-local",  # Ollama/vLLM ignore this or use dummy token
            base_url=self.base_url,
            timeout_seconds=timeout_seconds,
            allowed_hosts=self.allowed_hosts,
            client=client,
            provider_name="self-hosted",
        )

    async def generate(self, request: LLMRequest) -> LLMResponse:
        # If request doesn't supply an explicit api_key, ensure fallback dummy token is used
        if not request.api_key:
            request = LLMRequest(
                system_prompt=request.system_prompt,
                user_prompt=request.user_prompt,
                model_name=request.model_name,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                timeout_seconds=request.timeout_seconds,
                stop_sequences=request.stop_sequences,
                api_key="ollama-local",
                extra_headers=request.extra_headers,
            )
        return await self._adapter.generate(request)

    async def health_check(self) -> bool:
        try:
            return await self._adapter.health_check()
        except Exception:
            return False

    def supports_model(self, model_name: str) -> bool:
        return bool(model_name)

    async def close(self) -> None:
        await self._adapter.close()
