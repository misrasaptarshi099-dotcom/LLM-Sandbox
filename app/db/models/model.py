"""Model model — represents an LLM model offered by a provider.

BCNF Functional Dependencies:
id -> provider_id, model_name, active, created_at
(provider_id, model_name) -> id, active, created_at

Candidate keys: id (PK), (provider_id, model_name) (UNIQUE).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Model(Base):
    __tablename__ = "models"
    __table_args__ = (
        UniqueConstraint("provider_id", "model_name", name="uq_models_provider_model_name"),
        Index("idx_models_provider_active", "provider_id", "active"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    provider_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("providers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    model_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )
    active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Relationships — lazy="raise" prevents un-eager-loaded queries
    provider = relationship("Provider", back_populates="models", lazy="raise")
    bindings = relationship("ChallengeModelBinding", back_populates="model", lazy="raise")
