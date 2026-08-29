"""LLM Provider abstraction protocol, data models, exceptions, and security validators.

Rules §2, §3, Architecture §5, Implementation Plan Phase 5:
- All provider interactions flow through the LLMProvider protocol.
- Strict URL and host allowlists — no participant-controlled outbound fetch.
- Authorization headers and API keys are never logged or stored in exceptions.
- Normalized token usage and latency metrics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse


# --- Exceptions ---
class ProviderError(Exception):
    """Base exception for all provider-level errors."""

    def __init__(self, message: str, provider: str = "unknown", retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.retryable = retryable

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(provider={self.provider!r}, "
            f"retryable={self.retryable}, message={self.message!r})"
        )


class ProviderTimeoutError(ProviderError):
    """Provider request timed out (retryable with backoff)."""

    def __init__(
        self, message: str = "Provider request timed out", provider: str = "unknown"
    ) -> None:
        super().__init__(message=message, provider=provider, retryable=True)


class ProviderRateLimitError(ProviderError):
    """Provider rate limit exceeded (HTTP 429 - retryable with exponential backoff)."""

    def __init__(
        self,
        message: str = "Provider rate limit exceeded",
        provider: str = "unknown",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message=message, provider=provider, retryable=True)
        self.retry_after = retry_after


class ProviderAuthError(ProviderError):
    """Provider authentication failed (HTTP 401/403 - non-retryable)."""

    def __init__(
        self, message: str = "Provider authentication failed", provider: str = "unknown"
    ) -> None:
        super().__init__(message=message, provider=provider, retryable=False)


class ProviderInvalidRequestError(ProviderError):
    """Provider rejected the request as invalid (HTTP 400/422 - non-retryable)."""

    def __init__(
        self, message: str = "Invalid request to provider", provider: str = "unknown"
    ) -> None:
        super().__init__(message=message, provider=provider, retryable=False)


class ProviderUnavailableError(ProviderError):
    """Provider service temporarily unavailable (HTTP 500/502/503/504 - retryable)."""

    def __init__(
        self, message: str = "Provider service unavailable", provider: str = "unknown"
    ) -> None:
        super().__init__(message=message, provider=provider, retryable=True)


class ProviderSecurityError(ProviderError):
    """Provider URL or target host violated security allowlist (Rule §2)."""

    def __init__(
        self,
        message: str = "Security validation failed for provider endpoint",
        provider: str = "unknown",
    ) -> None:
        super().__init__(message=message, provider=provider, retryable=False)


# --- Data Models ---
@dataclass(frozen=True)
class LLMRequest:
    """Normalized input request to any LLM provider."""

    system_prompt: str
    user_prompt: str
    model_name: str
    temperature: float = 0.7
    max_tokens: int = 1000
    timeout_seconds: float = 30.0
    stop_sequences: list[str] = field(default_factory=list)
    api_key: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Never expose api_key or prompt text in string representation (Rule §2)."""
        return (
            f"LLMRequest(model_name={self.model_name!r}, "
            f"temperature={self.temperature}, max_tokens={self.max_tokens}, "
            f"timeout_seconds={self.timeout_seconds}, stop_sequences={self.stop_sequences}, "
            f"has_api_key={self.api_key is not None})"
        )


@dataclass(frozen=True)
class TokenUsage:
    """Normalized token consumption metrics."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    @classmethod
    def estimate(cls, prompt_text: str, completion_text: str) -> TokenUsage:
        """Heuristic fallback token estimator (~4 chars per token)."""
        p_tokens = max(1, len(prompt_text) // 4)
        c_tokens = max(1, len(completion_text) // 4)
        return cls(
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            total_tokens=p_tokens + c_tokens,
        )


@dataclass(frozen=True)
class LLMResponse:
    """Normalized completion output from any LLM provider."""

    text: str
    model_name: str
    usage: TokenUsage
    latency_ms: float
    raw_finish_reason: str | None = None

    def __repr__(self) -> str:
        return (
            f"LLMResponse(model_name={self.model_name!r}, "
            f"text_len={len(self.text)}, latency_ms={self.latency_ms:.2f}, "
            f"usage={self.usage}, finish_reason={self.raw_finish_reason!r})"
        )


# --- Security Validation ---
def validate_provider_url(
    url: str,
    allowed_hosts: set[str] | list[str] | None = None,
    allow_http_localhost: bool = True,
) -> str:
    """Validate that provider target URL satisfies security constraints (Rule §2).

    1. URL scheme must be HTTPS (or HTTP for localhost/127.0.0.1 if allowed).
    2. Host must be present in the allowed_hosts whitelist.
    3. User/Password in URL is strictly forbidden.
    """
    if not url or not isinstance(url, str):
        raise ProviderSecurityError("Provider base URL must be a non-empty string.")

    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.hostname:
        raise ProviderSecurityError(f"Invalid provider URL format: {url}")

    hostname = parsed.hostname.lower()

    # Explicit SSRF protection against cloud metadata services
    _blocked_metadata = {
        "169.254.169.254",
        "metadata.google.internal",
        "metadata.internal",
        "100.100.100.200",
    }
    if hostname in _blocked_metadata or hostname.endswith(".internal"):
        raise ProviderSecurityError(
            f"Access to cloud metadata or internal endpoint '{hostname}' is strictly forbidden."
        )

    if parsed.username or parsed.password:
        raise ProviderSecurityError("Embedded credentials in provider URL are strictly forbidden.")

    # Validate scheme
    if parsed.scheme == "http":
        if not (allow_http_localhost and hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ProviderSecurityError(
                "Insecure HTTP scheme is only permitted for local development endpoints, "
                f"got: {url}"
            )
    elif parsed.scheme != "https":
        raise ProviderSecurityError(f"Unsupported URL scheme: {parsed.scheme}")

    # Validate allowed hosts whitelist
    if not allowed_hosts:
        raise ProviderSecurityError("Allowed hosts whitelist is required and cannot be empty.")

    allowed_set = {h.lower() for h in allowed_hosts}
    if hostname not in allowed_set:
        raise ProviderSecurityError(
            f"Provider host '{hostname}' is not in the configured host allowlist."
        )

    return url.strip().rstrip("/")


# --- Protocol ---
@runtime_checkable
class LLMProvider(Protocol):
    """Abstract Protocol that all LLM provider adapters must implement."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Execute a text generation call to the model and return normalized response."""
        ...

    async def health_check(self) -> bool:
        """Check provider reachability/health."""
        ...

    def supports_model(self, model_name: str) -> bool:
        """Verify if provider supports the requested model."""
        ...

    async def close(self) -> None:
        """Clean up underlying HTTP connections/pools."""
        ...
