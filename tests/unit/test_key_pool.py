"""Unit tests for SlidingWindowKeyPool and multi-key rotation with preemption."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.providers.base import LLMRequest, ProviderRateLimitError
from app.providers.gemini import GeminiProvider
from app.providers.key_pool import SlidingWindowKeyPool


@pytest.mark.asyncio
async def test_key_pool_basic_acquisition() -> None:
    pool = SlidingWindowKeyPool(api_keys=["key1", "key2"], max_rpm=2)
    assert pool.total_keys == 2

    # Should acquire keys
    k1 = await pool.acquire_key()
    k2 = await pool.acquire_key()
    assert {k1, k2} == {"key1", "key2"}


@pytest.mark.asyncio
async def test_key_pool_prunes_and_rotates() -> None:
    # Use 0.1s window for ultra-fast test
    pool = SlidingWindowKeyPool(api_keys=["key1"], max_rpm=1, window_seconds=0.1)

    t0_key = await pool.acquire_key()
    assert t0_key == "key1"

    # Immediately requesting another key should wait ~0.1s until window opens
    t1_key = await pool.acquire_key()
    assert t1_key == "key1"


@pytest.mark.asyncio
async def test_key_pool_cooldown_on_rate_limit() -> None:
    pool = SlidingWindowKeyPool(api_keys=["key1", "key2"], max_rpm=10, cooldown_seconds=0.2)

    # Put key1 into cooldown
    await pool.report_rate_limit("key1")

    # Next acquire MUST be key2 because key1 is cooling down
    k = await pool.acquire_key()
    assert k == "key2"

    # Wait for cooldown to expire
    await asyncio.sleep(0.25)
    stats = await pool.get_stats()
    key1_stat = stats[0]
    assert key1_stat["is_cooling_down"] is False


@pytest.mark.asyncio
async def test_gemini_provider_multi_key_failover() -> None:
    calls: list[str] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-goog-api-key", "")
        calls.append(key)
        if key == "bad_key":
            return httpx.Response(429, text="Rate limit reached", headers={"retry-after": "1"})
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"parts": [{"text": "Hello from healthy key"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 5},
            },
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    # Configure provider with bad_key first, then good_key
    provider = GeminiProvider(api_key="bad_key,good_key", client=mock_client)

    req = LLMRequest(system_prompt="", user_prompt="test", model_name="gemini-3.5-flash-lite")
    resp = await provider.generate(req)

    assert resp.text == "Hello from healthy key"
    # Verify that bad_key was tried first, failed with 429, and good_key was tried immediately
    assert "bad_key" in calls
    assert "good_key" in calls


@pytest.mark.asyncio
async def test_gemini_provider_all_keys_exhausted_raises_429() -> None:
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="All keys exhausted")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    provider = GeminiProvider(api_key="keyA,keyB", client=mock_client)

    req = LLMRequest(system_prompt="", user_prompt="test", model_name="gemini-3.5-flash-lite")
    with pytest.raises(ProviderRateLimitError):
        await provider.generate(req)


@pytest.mark.asyncio
async def test_gemini_provider_model_cascade_fallback() -> None:
    attempted_models: list[str] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        # Extract model from URL
        for m in ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.6-flash"]:
            if m in url_str:
                attempted_models.append(m)
                if m == "gemini-3.5-flash-lite":
                    return httpx.Response(429, text="Model quota exhausted")
                if m == "gemini-3.1-flash-lite":
                    return httpx.Response(
                        200,
                        json={
                            "candidates": [
                                {
                                    "content": {"parts": [{"text": "Hello from 3.1 fallback"}]},
                                    "finishReason": "STOP",
                                }
                            ],
                            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 5},
                        },
                    )
        return httpx.Response(500, text="Unexpected model")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    provider = GeminiProvider(
        api_key="key1",
        client=mock_client,
        fallback_models=["gemini-3.1-flash-lite", "gemini-3.6-flash"],
    )

    req = LLMRequest(system_prompt="", user_prompt="test", model_name="gemini-3.5-flash-lite")
    resp = await provider.generate(req)

    assert resp.text == "Hello from 3.1 fallback"
    assert "gemini-3.5-flash-lite" in attempted_models
    assert "gemini-3.1-flash-lite" in attempted_models
