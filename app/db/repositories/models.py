"""Model and Provider repository."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models.model import Model
from app.db.models.provider import Provider


class ModelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_provider_by_code(self, code: str) -> Provider | None:
        stmt = select(Provider).where(Provider.code == code)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_model(self, provider_id: int, model_name: str) -> Model | None:
        stmt = select(Model).where(
            Model.provider_id == provider_id,
            Model.model_name == model_name,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_active_providers_with_models(self) -> list[Provider]:
        stmt = (
            select(Provider)
            .where(Provider.active.is_(True))
            .options(selectinload(Provider.models))
            .order_by(Provider.code)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
