"""Challenge repository — database access for challenges and version bindings.

Rules §2, §5:
- Never expose decrypted system prompt.
- Eager join to resolve challenge + latest version + allowed model bindings in 1 query (no N+1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models.challenge import Challenge
from app.db.models.challenge_model_binding import ChallengeModelBinding
from app.db.models.challenge_version import ChallengeVersion
from app.db.models.model import Model
from app.db.models.provider import Provider


@dataclass(frozen=True)
class ChallengeExecutionContext:
    """Resolved immutable context required for execution."""

    challenge: Challenge
    version: ChallengeVersion
    binding: ChallengeModelBinding
    model: Model
    provider: Provider


class ChallengeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_slug(self, slug: str) -> Challenge | None:
        """Fetch a challenge by slug."""
        stmt = select(Challenge).where(Challenge.slug == slug)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_live_binding(
        self,
        challenge_slug: str,
        preferred_model_name: str | None = None,
    ) -> ChallengeExecutionContext | None:
        """Resolve the latest published version and active model binding in 1 query."""
        stmt = (
            select(Challenge, ChallengeVersion, ChallengeModelBinding, Model, Provider)
            .join(ChallengeVersion, ChallengeVersion.challenge_id == Challenge.id)
            .join(
                ChallengeModelBinding,
                ChallengeModelBinding.challenge_version_id == ChallengeVersion.id,
            )
            .join(Model, Model.id == ChallengeModelBinding.model_id)
            .join(Provider, Provider.id == Model.provider_id)
            .where(
                Challenge.slug == challenge_slug,
                Challenge.status == "LIVE",
                ChallengeVersion.published_at.is_not(None),
                ChallengeModelBinding.active.is_(True),
                Model.active.is_(True),
                Provider.active.is_(True),
            )
            .order_by(ChallengeVersion.version_no.desc())
        )

        if preferred_model_name:
            stmt = stmt.where(Model.model_name == preferred_model_name)

        stmt = stmt.limit(1)
        result = await self.session.execute(stmt)
        row = result.first()
        if not row:
            return None

        challenge, version, binding, model, provider = row
        return ChallengeExecutionContext(
            challenge=challenge,
            version=version,
            binding=binding,
            model=model,
            provider=provider,
        )

    async def list_public_challenges(self) -> list[dict[str, Any]]:
        """List active public challenge metadata (safe for participants).

        Never includes system prompt ciphertext or decrypted secret prompt (Rule §2).
        Only includes published challenge versions.
        """
        stmt = (
            select(Challenge)
            .where(Challenge.status == "LIVE")
            .options(
                selectinload(Challenge.versions)
                .selectinload(ChallengeVersion.bindings)
                .selectinload(ChallengeModelBinding.model)
            )
            .order_by(Challenge.created_at.desc())
        )
        result = await self.session.execute(stmt)
        challenges = result.scalars().all()

        public_list: list[dict[str, Any]] = []
        for ch in challenges:
            published_versions = [v for v in ch.versions if v.published_at is not None]
            latest_version = published_versions[0] if published_versions else None
            allowed_models = []
            if latest_version:
                for b in latest_version.bindings:
                    if b.active and b.model.active:
                        allowed_models.append({
                            "model_name": b.model.model_name,
                            "max_input_tokens": b.max_input_tokens,
                            "max_output_tokens": b.max_output_tokens,
                            "temperature": float(b.temperature),
                        })

            public_list.append({
                "slug": ch.slug,
                "title": ch.title,
                "status": ch.status,
                "latest_version": latest_version.version_no if latest_version else 1,
                "allowed_models": allowed_models,
            })

        return public_list
