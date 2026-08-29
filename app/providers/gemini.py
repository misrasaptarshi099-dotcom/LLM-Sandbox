"""Gemini HTTP provider adapter (Google Generative AI REST API v1beta).

Rules §2, §3, Architecture §5, Implementation Plan Phase 5:
- Uses x-goog-api-key header (never exposes API key in URL query params or logs).
- Strict endpoint URL validation against host allowlist.
- Full error classification (429, 401/403, 400, 5xx, timeouts).
- Normalized LLMResponse output with accurate usage metrics.
"""

from __future__ import annotations

import contextlib
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
from app.providers.key_pool import SlidingWindowKeyPool

DEFAULT_FALLBACK_MODELS: list[str] = [
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
]


class GeminiProvider(LLMProvider):
    """Google Gemini REST API adapter with multi-key pool and model cascade fallback."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 30.0,
        allowed_hosts: list[str] | None = None,
        client: httpx.AsyncClient | None = None,
        max_rpm_per_key: int = 14,
        fallback_models: list[str] | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.gemini_api_key
        raw_base_url = base_url or settings.gemini_base_url
        self.allowed_hosts = allowed_hosts or settings.provider_allowed_hosts
        self.base_url = validate_provider_url(raw_base_url, self.allowed_hosts)
        self.timeout_seconds = timeout_seconds
        self.fallback_models = (
            fallback_models if fallback_models is not None else DEFAULT_FALLBACK_MODELS
        )

        # Multi-key sliding window quota pool
        raw_key_string = self.api_key or ""
        parsed_keys = [k.strip() for k in raw_key_string.split(",") if k.strip()]
        self.key_pool: SlidingWindowKeyPool | None = (
            SlidingWindowKeyPool(parsed_keys, max_rpm=max_rpm_per_key) if parsed_keys else None
        )

        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            event_hooks={"request": [create_dns_rebinding_validator(allow_http_localhost=False)]},
        )
        self._owned_client = client is None

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Execute request across healthy keys in pool, cascading to fallback models on 429."""
        if request.api_key:
            return await self._call_gemini_api(request, request.api_key)

        if not self.key_pool:
            raise ProviderAuthError(
                "Gemini API key is not configured or provided.",
                provider="gemini",
            )

        models_to_try = [request.model_name] + [
            m for m in self.fallback_models if m != request.model_name
        ]
        last_error: Exception | None = None

        for model_candidate in models_to_try:
            req = LLMRequest(
                system_prompt=request.system_prompt,
                user_prompt=request.user_prompt,
                model_name=model_candidate,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                stop_sequences=request.stop_sequences,
                timeout_seconds=request.timeout_seconds,
                api_key=request.api_key,
                extra_headers=request.extra_headers,
            )
            max_attempts = self.key_pool.total_keys
            for attempt_idx in range(max_attempts):
                key = await self.key_pool.acquire_key(
                    model=model_candidate,
                    wait_if_unavailable=False,
                )
                if key is None:
                    # All keys currently in cooldown for this model, try next fallback model
                    break
                try:
                    return await self._call_gemini_api(req, key)
                except ProviderRateLimitError as exc:
                    cooldown = exc.retry_after or 5.0
                    await self.key_pool.report_rate_limit(
                        key,
                        model=model_candidate,
                        cooldown_seconds=cooldown,
                    )
                    last_error = exc
                    if attempt_idx == max_attempts - 1:
                        # Move to the next fallback model candidate
                        break
                except Exception as exc:
                    last_error = exc
                    break

        # If all models are temporarily busy in the current 60s window,
        # wait for the earliest slot to open rather than failing the run!
        try:
            key = await self.key_pool.acquire_key(
                model=request.model_name,
                wait_if_unavailable=True,
            )
            if key is not None:
                return await self._call_gemini_api(request, key)
        except Exception as exc:
            last_error = exc

        if isinstance(last_error, ProviderError):
            raise last_error
        raise ProviderRateLimitError(
            "All Gemini API keys and fallback models exhausted their quota.",
            provider="gemini",
        )

    async def _call_gemini_api(self, request: LLMRequest, effective_key: str) -> LLMResponse:

        endpoint = f"{self.base_url}/models/{request.model_name}:generateContent"

        # Build payload according to Gemini v1beta schema
        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": request.user_prompt}],
                }
            ],
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_tokens,
            },
        }

        if request.system_prompt:
            payload["system_instruction"] = {"parts": [{"text": request.system_prompt}]}

        if request.stop_sequences:
            payload["generationConfig"]["stopSequences"] = request.stop_sequences

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": effective_key,
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
                f"Gemini API request timed out after {timeout}s: {exc}",
                provider="gemini",
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailableError(
                f"Gemini connection failure: {exc}",
                provider="gemini",
            ) from exc

        latency_ms = (time.perf_counter() - start_time) * 1000.0

        # Classify HTTP status codes
        if response.status_code == 429:
            retry_after: float | None = None
            retry_after_hdr = response.headers.get("retry-after")
            if retry_after_hdr and retry_after_hdr.isdigit():
                retry_after = float(retry_after_hdr)
            else:
                with contextlib.suppress(Exception):
                    err_json = response.json()
                    for detail in err_json.get("error", {}).get("details", []):
                        if "retryDelay" in detail:
                            delay_str = str(detail["retryDelay"]).rstrip("s")
                            retry_after = float(delay_str)
                            break
                    if retry_after is None:
                        import re

                        m = re.search(r"Please retry in ([0-9.]+)s", response.text)
                        if m:
                            retry_after = float(m.group(1))

            effective_delay = retry_after if retry_after_hdr else max(retry_after or 5.0, 5.0)
            raise ProviderRateLimitError(
                f"Gemini rate limit exceeded (HTTP 429): {response.text}",
                provider="gemini",
                retry_after=effective_delay,
            )
        if response.status_code in {401, 403}:
            raise ProviderAuthError(
                f"Gemini authentication failure (HTTP {response.status_code})",
                provider="gemini",
            )
        if response.status_code in {400, 422}:
            raise ProviderInvalidRequestError(
                f"Gemini rejected request as invalid (HTTP {response.status_code}): "
                f"{response.text}",
                provider="gemini",
            )
        if response.status_code >= 500:
            raise ProviderUnavailableError(
                f"Gemini server error (HTTP {response.status_code}): {response.text}",
                provider="gemini",
            )
        if response.status_code != 200:
            raise ProviderError(
                f"Gemini returned unexpected HTTP status {response.status_code}: {response.text}",
                provider="gemini",
            )

        data = response.json()
        return self._parse_gemini_response(data, request.model_name, latency_ms, request)

    def _parse_gemini_response(
        self,
        data: dict[str, Any],
        model_name: str,
        latency_ms: float,
        request: LLMRequest,
    ) -> LLMResponse:
        candidates = data.get("candidates", [])
        completion_text = ""
        finish_reason = None

        if candidates:
            first_cand = candidates[0]
            finish_reason = first_cand.get("finishReason")
            parts = first_cand.get("content", {}).get("parts", [])
            if parts:
                completion_text = "".join(part.get("text", "") for part in parts)

        # Extract usage metadata
        usage_meta = data.get("usageMetadata", {})
        prompt_tokens = usage_meta.get("promptTokenCount")
        completion_tokens = usage_meta.get("candidatesTokenCount")
        total_tokens = usage_meta.get("totalTokenCount")

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
        return bool(self.api_key)

    def supports_model(self, model_name: str) -> bool:
        return model_name.startswith("gemini-") or "gemini" in model_name.lower()

    async def close(self) -> None:
        if self._owned_client:
            await self._client.aclose()
