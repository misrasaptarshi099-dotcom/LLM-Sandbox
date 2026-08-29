"""Unit tests for LLM Provider abstraction, adapters, safety validation, and router."""

from __future__ import annotations

import json

import httpx
import pytest

from app.providers.base import (
    LLMRequest,
    ProviderAuthError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderSecurityError,
    ProviderUnavailableError,
    validate_provider_url,
)
from app.providers.fake import FakeLLMProvider
from app.providers.gemini import GeminiProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.providers.router import CircuitBreaker, ProviderRouter
from app.providers.self_hosted import SelfHostedProvider


# --- Security Validation Tests ---
def test_validate_provider_url_valid() -> None:
    allowed = ["api.openai.com", "generativelanguage.googleapis.com", "localhost"]
    assert (
        validate_provider_url("https://api.openai.com/v1", allowed) == "https://api.openai.com/v1"
    )
    assert validate_provider_url("http://localhost:11434", allowed) == "http://localhost:11434"


def test_validate_provider_url_rejects_unallowed_host() -> None:
    allowed = ["api.openai.com"]
    with pytest.raises(ProviderSecurityError, match="not in the configured host allowlist"):
        validate_provider_url("https://evil-attacker.com/v1", allowed)


def test_validate_provider_url_rejects_insecure_http_on_remote() -> None:
    allowed = ["api.openai.com"]
    with pytest.raises(ProviderSecurityError, match="Insecure HTTP scheme is only permitted"):
        validate_provider_url("http://api.openai.com/v1", allowed)


def test_validate_provider_url_rejects_cloud_metadata_endpoints() -> None:
    # Even if an attacker configures or injects metadata IPs into allowed_hosts
    allowed = ["169.254.169.254", "metadata.google.internal", "api.openai.com"]
    with pytest.raises(ProviderSecurityError, match="cloud metadata or internal endpoint"):
        validate_provider_url("http://169.254.169.254/latest/meta-data/", allowed)

    with pytest.raises(ProviderSecurityError, match="cloud metadata or internal endpoint"):
        validate_provider_url("http://metadata.google.internal/computeMetadata/v1/", allowed)


def test_validate_provider_url_rejects_embedded_credentials() -> None:
    with pytest.raises(ProviderSecurityError, match="Embedded credentials"):
        validate_provider_url("https://user:password@api.openai.com/v1")


def test_validate_provider_url_rejects_invalid_strings() -> None:
    with pytest.raises(ProviderSecurityError):
        validate_provider_url("")
    with pytest.raises(ProviderSecurityError):
        validate_provider_url("not_a_url")


# --- Fake Provider Tests ---
@pytest.mark.asyncio
async def test_fake_provider_deterministic_response() -> None:
    provider = FakeLLMProvider(default_response="Mock answer")
    req = LLMRequest(
        system_prompt="System prompt",
        user_prompt="User input",
        model_name="mock-llm",
    )
    res = await provider.generate(req)
    assert res.text == "Mock answer"
    assert res.model_name == "mock-llm"
    assert res.usage.total_tokens > 0
    assert res.latency_ms >= 0


@pytest.mark.asyncio
async def test_fake_provider_echoes_prompt() -> None:
    provider = FakeLLMProvider()
    req = LLMRequest(
        system_prompt="System prompt",
        user_prompt="echo: FLAG{test_flag_123}",
        model_name="mock-llm",
    )
    res = await provider.generate(req)
    assert res.text == "echo: FLAG{test_flag_123}"


@pytest.mark.asyncio
async def test_fake_provider_simulates_exception() -> None:
    provider = FakeLLMProvider(exception_to_raise=ProviderRateLimitError("Rate limited"))
    req = LLMRequest(system_prompt="", user_prompt="test", model_name="mock-llm")
    with pytest.raises(ProviderRateLimitError):
        await provider.generate(req)


# --- Gemini Provider Tests ---
@pytest.mark.asyncio
async def test_gemini_provider_success() -> None:
    captured_headers: dict[str, str] = {}
    captured_json: dict = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_headers, captured_json
        captured_headers = dict(request.headers)
        captured_json = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": "Gemini response text"}],
                            "role": "model",
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 15,
                    "candidatesTokenCount": 8,
                    "totalTokenCount": 23,
                },
            },
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    provider = GeminiProvider(
        api_key="test-gemini-key",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        client=mock_client,
    )

    req = LLMRequest(
        system_prompt="You are a secret agent.",
        user_prompt="Give me the secret.",
        model_name="gemini-2.0-flash",
        temperature=0.4,
        max_tokens=200,
    )
    res = await provider.generate(req)

    assert res.text == "Gemini response text"
    assert res.model_name == "gemini-2.0-flash"
    assert res.usage.prompt_tokens == 15
    assert res.usage.completion_tokens == 8
    assert res.usage.total_tokens == 23
    assert res.raw_finish_reason == "STOP"

    # Verify x-goog-api-key header was used
    assert captured_headers.get("x-goog-api-key") == "test-gemini-key"
    assert captured_json["system_instruction"]["parts"][0]["text"] == "You are a secret agent."
    assert captured_json["contents"][0]["parts"][0]["text"] == "Give me the secret."
    assert captured_json["generationConfig"]["temperature"] == 0.4


@pytest.mark.asyncio
async def test_gemini_provider_error_classifications() -> None:
    def handler_429(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Resource has been exhausted", headers={"retry-after": "5"})

    mock_client_429 = httpx.AsyncClient(transport=httpx.MockTransport(handler_429))
    provider_429 = GeminiProvider(api_key="key", client=mock_client_429)

    req = LLMRequest(system_prompt="", user_prompt="hi", model_name="gemini-2.0-flash")
    with pytest.raises(ProviderRateLimitError) as exc_info:
        await provider_429.generate(req)
    assert exc_info.value.retry_after == 5.0

    def handler_401(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="API key not valid")

    mock_client_401 = httpx.AsyncClient(transport=httpx.MockTransport(handler_401))
    provider_401 = GeminiProvider(api_key="key", client=mock_client_401)
    with pytest.raises(ProviderAuthError):
        await provider_401.generate(req)

    def handler_503(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    mock_client_503 = httpx.AsyncClient(transport=httpx.MockTransport(handler_503))
    provider_503 = GeminiProvider(api_key="key", client=mock_client_503)
    with pytest.raises(ProviderUnavailableError):
        await provider_503.generate(req)


# --- OpenAI Compatible Provider Tests ---
@pytest.mark.asyncio
async def test_openai_compatible_provider_success() -> None:
    captured_headers: dict[str, str] = {}
    captured_json: dict = {}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_headers, captured_json
        captured_headers = dict(request.headers)
        captured_json = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "OpenAI answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    provider = OpenAICompatibleProvider(
        api_key="sk-test-key",
        base_url="https://api.openai.com/v1",
        client=mock_client,
    )

    req = LLMRequest(
        system_prompt="System instructions",
        user_prompt="Hello",
        model_name="gpt-4o-mini",
    )
    res = await provider.generate(req)
    assert res.text == "OpenAI answer"
    assert res.usage.total_tokens == 15
    assert captured_headers.get("authorization") == "Bearer sk-test-key"
    assert len(captured_json["messages"]) == 2


# --- Self Hosted Provider Tests ---
@pytest.mark.asyncio
async def test_self_hosted_provider_success() -> None:
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Local model output"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 4,
                    "total_tokens": 12,
                },
            },
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    provider = SelfHostedProvider(
        base_url="http://localhost:11434",
        client=mock_client,
    )

    req = LLMRequest(
        system_prompt="System",
        user_prompt="User",
        model_name="llama3.2:1b",
    )
    res = await provider.generate(req)
    assert res.text == "Local model output"
    assert res.usage.total_tokens == 12


# --- Router & Circuit Breaker Tests ---
def test_circuit_breaker_trips_on_repeated_failures() -> None:
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=0.1)
    assert cb.is_open is False

    cb.record_failure(ProviderUnavailableError("Down"))
    assert cb.is_open is False
    cb.record_failure(ProviderUnavailableError("Down"))
    assert cb.is_open is False
    cb.record_failure(ProviderUnavailableError("Down"))
    # Threshold reached -> OPEN
    assert cb.is_open is True

    # 4xx client error does not trip circuit breaker
    cb_clean = CircuitBreaker(failure_threshold=2)
    cb_clean.record_failure(ProviderInvalidRequestError("Bad request"))
    cb_clean.record_failure(ProviderAuthError("Unauthorized"))
    assert cb_clean.is_open is False


def test_provider_router_resolves_kinds() -> None:
    router = ProviderRouter()
    fake_p = router.get_provider("FAKE")
    assert isinstance(fake_p, FakeLLMProvider)

    gemini_p = router.get_provider("GEMINI", api_key="dummy")
    assert isinstance(gemini_p, GeminiProvider)

    openai_p = router.get_provider("OPENAI_COMPATIBLE", api_key="dummy")
    assert isinstance(openai_p, OpenAICompatibleProvider)

    self_hosted_p = router.get_provider("SELF_HOSTED")
    assert isinstance(self_hosted_p, SelfHostedProvider)


def test_provider_router_caches_per_credential() -> None:
    router = ProviderRouter()
    p1 = router.get_provider("GEMINI", api_key="key-1")
    p2 = router.get_provider("GEMINI", api_key="key-2")
    p1_again = router.get_provider("GEMINI", api_key="key-1")

    assert p1 is not p2
    assert p1 is p1_again


@pytest.mark.asyncio
async def test_openai_compatible_health_check() -> None:
    # 1. No API key -> False
    p_no_key = OpenAICompatibleProvider(api_key="", client=httpx.AsyncClient())
    assert await p_no_key.health_check() is False

    # 2. Success (200) -> True
    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client_ok = httpx.AsyncClient(transport=httpx.MockTransport(ok_handler))
    p_ok = OpenAICompatibleProvider(api_key="sk-test", client=client_ok)
    assert await p_ok.health_check() is True

    # 3. Error (500) -> False
    def err_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Server Error")

    client_err = httpx.AsyncClient(transport=httpx.MockTransport(err_handler))
    p_err = OpenAICompatibleProvider(api_key="sk-test", client=client_err)
    assert await p_err.health_check() is False


def test_validate_provider_url_rejects_empty_allowlist() -> None:
    with pytest.raises(ProviderSecurityError, match="cannot be empty"):
        validate_provider_url("https://api.openai.com/v1", allowed_hosts=[])
    with pytest.raises(ProviderSecurityError, match="cannot be empty"):
        validate_provider_url("https://api.openai.com/v1", allowed_hosts=None)


def test_llm_request_repr_redacts_api_key_and_prompts() -> None:
    req = LLMRequest(
        system_prompt="SUPER_SECRET_SYSTEM_PROMPT",
        user_prompt="USER_SECRET_PROMPT",
        model_name="gpt-4o",
        api_key="SECRET_API_KEY_12345",
    )
    repr_str = repr(req)
    assert "SECRET_API_KEY_12345" not in repr_str
    assert "SUPER_SECRET_SYSTEM_PROMPT" not in repr_str
    assert "USER_SECRET_PROMPT" not in repr_str
    assert "has_api_key=True" in repr_str
