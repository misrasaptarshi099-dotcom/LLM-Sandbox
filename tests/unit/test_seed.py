from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.seed_challenge import seed


@pytest.mark.asyncio
async def test_seed_script_runs_without_errors() -> None:
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    test_session_factory = async_sessionmaker(
        bind=test_engine,
        expire_on_commit=False,
    )

    # Running seed() twice must be idempotent
    await seed(custom_engine=test_engine, custom_session_factory=test_session_factory)
    await seed(custom_engine=test_engine, custom_session_factory=test_session_factory)

    await test_engine.dispose()
