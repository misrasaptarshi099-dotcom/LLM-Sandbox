"""Integration tests for cross-user authorization and tenant isolation.

PRD §11 Multi-user isolation:
- A run may access only its own result.
- No participant can view or select another participant's run ID or internal details.
- User queries are strictly scoped to the authenticated user.
- Unauthenticated requests in production or invalid/tampered tokens are rejected with 401.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_session
from app.core.config import Settings
from app.db.models.user import User
from app.main import app


@pytest.mark.asyncio
async def test_cross_user_run_status_access_forbidden_404(
    client: AsyncClient,
    seeded_test_env: dict,
    test_db_session: AsyncSession,
) -> None:
    """User B attempting to view User A's run receives 404 (isolation by design)."""
    user_a = seeded_test_env["user"]

    # 1. User A submits a run
    res = await client.post(
        "/v1/runs",
        json={
            "challenge_slug": "prompt-injection-01",
            "prompt": "User A secret prompt",
        },
    )
    assert res.status_code == 202
    run_id = res.json()["run_id"]

    # 2. Create User B in DB
    user_b = User(
        external_ref=f"user_b_{uuid.uuid4().hex[:8]}",
        display_name="Participant B",
    )
    test_db_session.add(user_b)
    await test_db_session.commit()

    # 3. Simulate User B authentication
    app.dependency_overrides[get_current_user] = lambda: user_b
    try:
        status_res = await client.get(f"/v1/runs/{run_id}")
        # Must return 404 so existence of foreign runs is not leaked
        assert status_res.status_code == 404
        data = status_res.json()
        assert data["error"]["code"] == "NOT_FOUND"
        assert f"Run '{run_id}' not found" in data["error"]["message"]
    finally:
        # Reset override back to User A
        app.dependency_overrides[get_current_user] = lambda: user_a


@pytest.mark.asyncio
async def test_user_runs_history_is_isolated(
    client: AsyncClient,
    seeded_test_env: dict,
    test_db_session: AsyncSession,
) -> None:
    """User A and User B cannot see each other's runs in history listings."""
    user_a = seeded_test_env["user"]

    # User A submits a run
    res_a = await client.post(
        "/v1/runs",
        json={
            "challenge_slug": "prompt-injection-01",
            "prompt": "User A history item",
        },
    )
    assert res_a.status_code == 202
    run_a_id = res_a.json()["run_id"]

    # Create User B
    user_b = User(
        external_ref=f"user_b_{uuid.uuid4().hex[:8]}",
        display_name="Participant B",
    )
    test_db_session.add(user_b)
    await test_db_session.commit()

    # Switch to User B
    app.dependency_overrides[get_current_user] = lambda: user_b
    try:
        # User B submits a run
        res_b = await client.post(
            "/v1/runs",
            json={
                "challenge_slug": "prompt-injection-01",
                "prompt": "User B history item",
            },
        )
        assert res_b.status_code == 202
        run_b_id = res_b.json()["run_id"]

        # User B queries history: must see only run_b_id
        history_b = await client.get("/v1/runs")
        assert history_b.status_code == 200
        items_b = history_b.json()["runs"]
        ids_b = [item["id"] for item in items_b]
        assert run_b_id in ids_b
        assert run_a_id not in ids_b
    finally:
        app.dependency_overrides[get_current_user] = lambda: user_a

    # User A queries history: must see only run_a_id
    history_a = await client.get("/v1/runs")
    assert history_a.status_code == 200
    items_a = history_a.json()["runs"]
    ids_a = [item["id"] for item in items_a]
    assert run_a_id in ids_a


@pytest.mark.asyncio
async def test_invalid_token_rejected_401(
    test_db_session: AsyncSession,
) -> None:
    """Invalid or tampered Authorization header returns 401."""
    overrides_backup = dict(app.dependency_overrides)
    if get_current_user in app.dependency_overrides:
        del app.dependency_overrides[get_current_user]

    app.dependency_overrides[get_db_session] = lambda: test_db_session

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as anon_client:
            res = await anon_client.get(
                "/v1/runs",
                headers={"Authorization": "Bearer totally-invalid-token-12345"},
            )
            assert res.status_code == 401
            data = res.json()
            assert "Invalid or expired authentication token" in data["error"]["message"]
    finally:
        app.dependency_overrides = overrides_backup


@pytest.mark.asyncio
async def test_unauthenticated_request_in_production_rejected_401(
    test_db_session: AsyncSession,
) -> None:
    """In production environment, unauthenticated requests are rejected with 401."""
    overrides_backup = dict(app.dependency_overrides)
    if get_current_user in app.dependency_overrides:
        del app.dependency_overrides[get_current_user]

    app.dependency_overrides[get_db_session] = lambda: test_db_session

    prod_settings = Settings(
        app_env="production",
        secret_key="prod-secure-key-must-be-at-least-32-chars-long",
        dev_auth_token="prod-secure-dev-auth-token-32chars-min",
        aes_256_gcm_secret="prod-secure-aes-gcm-secret-key-32chars!",
    )

    try:
        with patch("app.api.deps.get_settings", return_value=prod_settings):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
            ) as anon_client:
                res = await anon_client.get("/v1/runs")
                assert res.status_code == 401
                data = res.json()
                assert "Missing required authentication credentials" in data["error"]["message"]
    finally:
        app.dependency_overrides = overrides_backup
