"""Provider model — represents an LLM vendor or self-hosted endpoint.

BCNF Functional Dependencies:
id -> code, kind, active
code -> id, kind, active

Candidate keys: id (PK), code (UNIQUE).
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    code: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )  # e.g., 'OPENAI_COMPATIBLE', 'SELF_HOSTED', 'FAKE'
    active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    # Relationships — lazy="raise" prevents un-eager-loaded queries
    models = relationship(
        "Model",
        back_populates="provider",
        cascade="all, delete-orphan",
        lazy="raise",
    )
