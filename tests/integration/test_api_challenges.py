"""Integration tests for Challenge API endpoints."""

from __future__ import annotations

from httpx import AsyncClient


async def test_get_challenges_returns_sanitized_public_list(
    client: AsyncClient, seeded_test_env: dict
) -> None:
    response = await client.get("/v1/challenges")
    assert response.status_code == 200

    data = response.json()
    assert "challenges" in data
    assert len(data["challenges"]) == 1

    ch = data["challenges"][0]
    assert ch["slug"] == "prompt-injection-01"
    assert ch["title"] == "TechnoVIT Flag Defense Level 1"
    assert ch["status"] == "LIVE"
    assert ch["latest_version"] == 1
    assert len(ch["allowed_models"]) == 1
    assert ch["allowed_models"][0]["model_name"] == "mock-llm"
    assert ch["allowed_models"][0]["max_input_tokens"] == 2048

    # Security check: verify zero secret/prompt leakage (Rule §2, PRD §8)
    raw_response_text = response.text
    assert "ciphertext" not in raw_response_text
    assert "system_prompt" not in raw_response_text
    assert "TECHNOVIT" not in raw_response_text
