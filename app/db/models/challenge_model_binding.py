"""ChallengeModelBinding model — binds a challenge version to a permitted model configuration.

BCNF Functional Dependencies:
id -> challenge_version_id, model_id, max_input_tokens, max_output_tokens,
      temperature, timeout_ms, active
(challenge_version_id, model_id) -> id, max_input_tokens, max_output_tokens,
                                    temperature, timeout_ms, active

Candidate keys: id (PK), (challenge_version_id, model_id) (UNIQUE).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    Numeric,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ChallengeModelBinding(Base):
    __tablename__ = "challenge_model_bindings"
    __table_args__ = (
        UniqueConstraint(
            "challenge_version_id",
            "model_id",
            name="uq_challenge_model_bindings_version_model",
        ),
        CheckConstraint("max_input_tokens > 0", name="max_input_tokens"),
        CheckConstraint("max_output_tokens > 0", name="max_output_tokens"),
        CheckConstraint("temperature >= 0", name="temperature"),
        CheckConstraint("timeout_ms > 0", name="timeout_ms"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    challenge_version_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("challenge_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    model_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("models.id", ondelete="RESTRICT"),
        nullable=False,
    )
    max_input_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=2048,
    )
    max_output_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=512,
    )
    temperature: Mapped[Decimal] = mapped_column(
        Numeric(4, 3),
        nullable=False,
        default=Decimal("0.700"),
    )
    timeout_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=15000,
    )
    active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    # Relationships
    challenge_version = relationship("ChallengeVersion", back_populates="bindings")
    model = relationship("Model", back_populates="bindings")
    runs = relationship("Run", back_populates="model_binding")
