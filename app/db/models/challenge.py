"""Challenge model — represents a prompt injection challenge identity.

BCNF Functional Dependencies:
id -> slug, title, status, created_at
slug -> id, title, status, created_at

Candidate keys: id (PK), slug (UNIQUE).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Challenge(Base):
    __tablename__ = "challenges"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT', 'LIVE', 'DISABLED', 'ARCHIVED')",
            name="status",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    slug: Mapped[str] = mapped_column(
        String(80),
        unique=True,
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="LIVE",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Relationships — lazy="raise" prevents un-eager-loaded queries
    versions = relationship(
        "ChallengeVersion",
        back_populates="challenge",
        cascade="all, delete-orphan",
        order_by="desc(ChallengeVersion.version_no)",
        lazy="raise",
    )
