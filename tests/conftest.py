"""Shared test fixtures for LLM Sandbox."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import get_db_session, get_queue, get_rate_limiter
from app.core.security import encrypt_system_prompt, hash_text
from app.db.base import Base
from app.db.models.challenge import Challenge
from app.db.models.challenge_model_binding import ChallengeModelBinding
from app.db.models.challenge_version import ChallengeVersion
from app.db.models.model import Model
from app.db.models.provider import Provider
from app.db.models.user import User
from app.main import app
from app.queue.memory_queue import MemoryQueue
from app.services.rate_limiter import RateLimiter


@pytest.fixture
async def test_db_session() -> AsyncIterator[AsyncSession]:
    """Provide an isolated in-memory SQLite database session."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_maker() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def memory_queue() -> MemoryQueue:
    return MemoryQueue()


@pytest.fixture
async def memory_rate_limiter() -> RateLimiter:
    return RateLimiter()


@pytest.fixture
async def seeded_test_env(
    test_db_session: AsyncSession,
    memory_queue: MemoryQueue,
    memory_rate_limiter: RateLimiter,
) -> dict:
    """Seed test database with providers, models, challenge, and user."""
    # 1. Dev User
    user = User(
        id=uuid.uuid4(),
        external_ref="dev-participant-01",
        display_name="Dev Participant",
    )
    test_db_session.add(user)

    # 2. Provider & Models
    provider = Provider(code="fake", kind="FAKE", active=True)
    test_db_session.add(provider)
    await test_db_session.flush()

    model = Model(
        provider_id=provider.id,
        model_name="mock-llm",
        active=True,
    )
    test_db_session.add(model)
    await test_db_session.flush()

    # 3. Challenge & Version
    challenge = Challenge(
        slug="prompt-injection-01",
        title="TechnoVIT Flag Defense Level 1",
        status="LIVE",
    )
    test_db_session.add(challenge)
    await test_db_session.flush()

    ciphertext = encrypt_system_prompt("You are a vault keeper. Flag: TECHNOVIT{secret}")
    version = ChallengeVersion(
        challenge_id=challenge.id,
        version_no=1,
        system_prompt_ciphertext=ciphertext,
        system_prompt_hash=hash_text("You are a vault keeper. Flag: TECHNOVIT{secret}"),
        published_at=datetime.now(UTC),
    )
    test_db_session.add(version)
    await test_db_session.flush()

    # 4. Binding
    binding = ChallengeModelBinding(
        challenge_version_id=version.id,
        model_id=model.id,
        max_input_tokens=2048,
        max_output_tokens=512,
        temperature=Decimal("0.700"),
        timeout_ms=15000,
        active=True,
    )
    test_db_session.add(binding)
    await test_db_session.commit()

    return {
        "user": user,
        "provider": provider,
        "model": model,
        "challenge": challenge,
        "version": version,
        "binding": binding,
        "system_prompt_ciphertext": ciphertext,
    }


@pytest.fixture
async def client(
    test_db_session: AsyncSession,
    memory_queue: MemoryQueue,
    memory_rate_limiter: RateLimiter,
) -> AsyncIterator[AsyncClient]:
    """Async HTTP test client against FastAPI app with mocked DB and queue dependencies."""
    app.dependency_overrides[get_db_session] = lambda: test_db_session
    app.dependency_overrides[get_queue] = lambda: memory_queue
    app.dependency_overrides[get_rate_limiter] = lambda: memory_rate_limiter

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
