"""Database connection and async session management.

Rules §5, Backend Structure §8:
- Connection pooling with bounded limits (pool_size=10, max_overflow=5, timeout=2s).
- Short transactions: commit or rollback explicitly, never hold open during LLM calls.
- Fast URL normalization (e.g. Railway postgres:// -> postgresql+asyncpg://).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


def normalize_database_url(url: str) -> str:
    """Ensure database URL uses the asyncpg driver for PostgreSQL."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://") and not url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def create_engine_from_url(url: str | None = None) -> AsyncEngine:
    """Create a configured async SQLAlchemy engine."""
    db_url = normalize_database_url(url or get_settings().database_url)

    engine_kwargs: dict = {
        "echo": False,
    }

    # Apply connection pooling options for PostgreSQL/network databases
    if not db_url.startswith("sqlite"):
        engine_kwargs.update({
            "pool_size": 10,
            "max_overflow": 5,
            "pool_timeout": 5.0,
            "pool_pre_ping": True,
        })

    return create_async_engine(db_url, **engine_kwargs)


# Module-level engine and sessionmaker singleton
engine: AsyncEngine = create_engine_from_url()
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a managed async database session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
