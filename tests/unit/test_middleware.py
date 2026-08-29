"""Unit tests for RequestSizeLimitMiddleware."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.middleware import RequestSizeLimitMiddleware


@pytest.fixture
def test_app() -> FastAPI:
    app = FastAPI()
    # Configure middleware with small 100-byte limit for testing
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=100)

    @app.post("/test")
    async def echo(payload: dict):
        return {"status": "ok", "size": len(str(payload))}

    return app


@pytest.mark.asyncio
async def test_request_under_size_limit_accepted(test_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/test", json={"msg": "hello"})
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_request_content_length_exceeded_rejected_413(test_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as client:
        headers = {"Content-Length": "500", "Content-Type": "application/json"}
        response = await client.post(
            "/test",
            content=b'{"msg": "overflow"}',
            headers=headers,
        )
        assert response.status_code == 413
        data = response.json()
        assert data["error"]["code"] == "REQUEST_ENTITY_TOO_LARGE"
        assert "500 bytes indicated" in data["error"]["message"]


@pytest.mark.asyncio
async def test_request_actual_body_exceeded_rejected_413(test_app: FastAPI) -> None:
    async def chunk_generator():
        yield b"X" * 75
        yield b"X" * 75

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/test",
            content=chunk_generator(),
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 413
        data = response.json()
        assert data["error"]["code"] == "REQUEST_ENTITY_TOO_LARGE"
        assert "150 bytes received" in data["error"]["message"]
