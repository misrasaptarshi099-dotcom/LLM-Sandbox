"""OpenAI-compatible HTTP provider adapter (OpenAI, Groq, Together, DeepSeek, OpenRouter).

Rules §2, §3, Architecture §5, Implementation Plan Phase 5:
- Uses standard /v1/chat/completions JSON schema.
- Authorization header is never logged or exposed.
- Validates endpoint base URL against host allowlist.
- Full error classification and normalized responses.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.core.config import get_settings
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
    TokenUsage,
    create_dns_rebinding_validator,
    validate_provider_url,
)


class OpenAICompatibleProvider(LLMProvider):
    """Generic OpenAI-compatible HTTP adapter."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 30.0,
        allowed_hosts: list[str] | None = None,
        client: httpx.AsyncClient | None = None,
        provider_name: str = "openai",
    ) -> None:
        settings = get_settings()
        self.provider_name = provider_name
        self.api_key = api_key or settings.openai_api_key
        raw_base_url = base_url or settings.openai_base_url
        self.allowed_hosts = allowed_hosts or settings.provider_allowed_hosts
        self.base_url = validate_provider_url(raw_base_url, self.allowed_hosts)
        self.timeout_seconds = timeout_seconds

        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            event_hooks={"request": [create_dns_rebinding_validator(allow_http_localhost=True)]},
        )
        self._owned_client = client is None

    async def generate(self, request: LLMRequest) -> LLMResponse:
        effective_key = request.api_key or self.api_key
        if not effective_key:
            raise ProviderAuthError(
                f"{self.provider_name.capitalize()} API key is not configured or provided.",
                provider=self.provider_name,
            )

        endpoint = f"{self.base_url}/chat/completions"

        messages: list[dict[str, str]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.user_prompt})

        payload: dict[str, Any] = {
            "model": request.model_name,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.stop_sequences:
            payload["stop"] = request.stop_sequences

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {effective_key}",
        }
        if request.extra_headers:
            headers.update(request.extra_headers)

        start_time = time.perf_counter()
        timeout = request.timeout_seconds or self.timeout_seconds

        try:
            response = await self._client.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"{self.provider_name.capitalize()} API request timed out after {timeout}s: {exc}",
                provider=self.provider_name,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError(
                f"{self.provider_name.capitalize()} connection failure: {exc}",
                provider=self.provider_name,
            ) from exc

        latency_ms = (time.perf_counter() - start_time) * 1000.0

        if response.status_code == 429:
            retry_after_hdr = response.headers.get("retry-after")
            retry_after = (
                float(retry_after_hdr) if retry_after_hdr and retry_after_hdr.isdigit() else None
            )
            name = self.provider_name.capitalize()
            raise ProviderRateLimitError(
                f"{name} rate limit exceeded (HTTP 429): {response.text}",
                provider=self.provider_name,
                retry_after=retry_after,
            )
        if response.status_code in {401, 403}:
            name = self.provider_name.capitalize()
            raise ProviderAuthError(
                f"{name} authentication failure (HTTP {response.status_code})",
                provider=self.provider_name,
            )
        if response.status_code in {400, 422}:
            name = self.provider_name.capitalize()
            raise ProviderInvalidRequestError(
                f"{name} rejected request (HTTP {response.status_code}): {response.text}",
                provider=self.provider_name,
            )
        if response.status_code >= 500:
            name = self.provider_name.capitalize()
            raise ProviderUnavailableError(
                f"{name} server error (HTTP {response.status_code}): {response.text}",
                provider=self.provider_name,
            )
        if response.status_code != 200:
            name = self.provider_name.capitalize()
            raise ProviderError(
                f"{name} unexpected HTTP status {response.status_code}: {response.text}",
                provider=self.provider_name,
            )

        data = response.json()
        return self._parse_openai_response(data, request.model_name, latency_ms, request)

    def _parse_openai_response(
        self,
        data: dict[str, Any],
        model_name: str,
        latency_ms: float,
        request: LLMRequest,
    ) -> LLMResponse:
        choices = data.get("choices", [])
        completion_text = ""
        finish_reason = None

        if choices:
            first_choice = choices[0]
            finish_reason = first_choice.get("finish_reason")
            message = first_choice.get("message", {})
            completion_text = message.get("content") or ""

        usage_dict = data.get("usage", {})
        prompt_tokens = usage_dict.get("prompt_tokens")
        completion_tokens = usage_dict.get("completion_tokens")
        total_tokens = usage_dict.get("total_tokens")

        if prompt_tokens is not None and completion_tokens is not None and total_tokens is not None:
            usage = TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
        else:
            usage = TokenUsage.estimate(
                prompt_text=f"{request.system_prompt} {request.user_prompt}",
                completion_text=completion_text,
            )

        return LLMResponse(
            text=completion_text,
            model_name=model_name,
            usage=usage,
            latency_ms=latency_ms,
            raw_finish_reason=finish_reason,
        )

    async def health_check(self) -> bool:
        if not self.api_key:
            return False
        endpoint = f"{self.base_url}/models"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }
        try:
            response = await self._client.get(
                endpoint,
                headers=headers,
                timeout=min(self.timeout_seconds, 5.0),
            )
            return response.status_code == 200
        except (httpx.RequestError, httpx.TimeoutException, Exception):
            return False

    def supports_model(self, model_name: str) -> bool:
        return bool(model_name)

    async def close(self) -> None:
        if self._owned_client:
            await self._client.aclose()
