"""Challenge API endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.challenges import ChallengeRepository
from app.db.session import get_db_session
from app.schemas.challenges import ChallengeListResponse, ChallengePublicResponse

router = APIRouter(prefix="/v1/challenges", tags=["challenges"])


@router.get("", response_model=ChallengeListResponse)
async def list_challenges(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ChallengeListResponse:
    """List active public challenges.

    Never exposes decrypted system prompts or ciphertext secrets (Rule §2).
    """
    repo = ChallengeRepository(session)
    challenges_raw = await repo.list_public_challenges()
    return ChallengeListResponse(
        challenges=[ChallengePublicResponse(**ch) for ch in challenges_raw]
    )
