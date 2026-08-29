"""FastAPI Dependency Injection Providers."""

from __future__ import annotations

import uuid
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import verify_auth_token
from app.db.models.user import User
from app.db.session import get_db_session
from app.queue.base import AbstractQueue
from app.queue.memory_queue import MemoryQueue
from app.queue.redis_queue import RedisQueue
from app.services.admission import AdmissionService
from app.services.cost_tracker import CostTracker
from app.services.rate_limiter import RateLimiter

# Module singletons for queue, rate limiter, and cost tracker
_queue_singleton: AbstractQueue | None = None
_rate_limiter_singleton: RateLimiter | None = None
_cost_tracker_singleton: CostTracker | None = None


def get_queue() -> AbstractQueue:
    """Return the global queue instance."""
    global _queue_singleton
    if _queue_singleton is None:
        try:
            _queue_singleton = RedisQueue()
        except Exception:
            _queue_singleton = MemoryQueue()
    return _queue_singleton


def set_queue(queue: AbstractQueue) -> None:
    """Override queue instance (e.g. in tests)."""
    global _queue_singleton
    _queue_singleton = queue


def get_rate_limiter() -> RateLimiter:
    """Return the global rate limiter instance."""
    global _rate_limiter_singleton
    if _rate_limiter_singleton is None:
        settings = get_settings()
        try:
            client = aioredis.from_url(settings.redis_url, decode_responses=True)
            _rate_limiter_singleton = RateLimiter(redis_client=client)
        except Exception:
            _rate_limiter_singleton = RateLimiter()
    return _rate_limiter_singleton


def set_rate_limiter(limiter: RateLimiter) -> None:
    """Override rate limiter instance (e.g. in tests)."""
    global _rate_limiter_singleton
    _rate_limiter_singleton = limiter


def get_cost_tracker() -> CostTracker:
    """Return the global cost tracker instance."""
    global _cost_tracker_singleton
    if _cost_tracker_singleton is None:
        settings = get_settings()
        try:
            client = aioredis.from_url(settings.redis_url, decode_responses=True)
            _cost_tracker_singleton = CostTracker(redis_client=client)
        except Exception:
            _cost_tracker_singleton = CostTracker()
    return _cost_tracker_singleton


def set_cost_tracker(tracker: CostTracker) -> None:
    """Override cost tracker instance (e.g. in tests)."""
    global _cost_tracker_singleton
    _cost_tracker_singleton = tracker


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    authorization: Annotated[str | None, Header()] = None,
    x_participant_id: Annotated[str | None, Header()] = None,
) -> User:
    """Authenticate and resolve the current user.

    Supports:
    1. Authorization: Bearer <dev_auth_token> (Required in production)
    2. X-Participant-Id: <external_ref> (Allowed in dev/test only)
    """
    settings = get_settings()
    is_dev_env = settings.app_env in ("development", "dev", "test", "testing")

    external_ref: str
    if authorization:
        if not verify_auth_token(authorization):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired authentication token.",
            )
        external_ref = (
            x_participant_id if (x_participant_id and is_dev_env) else "authenticated-participant"
        )
    elif is_dev_env:
        external_ref = x_participant_id or "dev-participant-01"
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing required authentication credentials.",
        )

    # Fetch or auto-provision participant record
    stmt = select(User).where(User.external_ref == external_ref)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        user = User(
            id=uuid.uuid4(),
            external_ref=external_ref,
            display_name=f"Participant {external_ref[:8]}",
        )
        session.add(user)
        await session.flush()

    return user


def get_admission_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    queue: Annotated[AbstractQueue, Depends(get_queue)],
    rate_limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    cost_tracker: Annotated[CostTracker, Depends(get_cost_tracker)],
) -> AdmissionService:
    """Provide initialized AdmissionService."""
    return AdmissionService(
        session=session,
        queue=queue,
        rate_limiter=rate_limiter,
        cost_tracker=cost_tracker,
    )
