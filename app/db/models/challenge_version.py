"""ChallengeVersion model — represents an immutable version of a challenge.

BCNF Functional Dependencies:
id -> challenge_id, version_no, system_prompt_ciphertext, system_prompt_hash,
      created_at, published_at
(challenge_id, version_no) -> id, system_prompt_ciphertext, system_prompt_hash,
                              created_at, published_at

Candidate keys: id (PK), (challenge_id, version_no) (UNIQUE).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ChallengeVersion(Base):
    __tablename__ = "challenge_versions"
    __table_args__ = (
        UniqueConstraint(
            "challenge_id", "version_no", name="uq_challenge_versions_challenge_version"
        ),
        Index("idx_challenge_versions_challenge", "challenge_id", "version_no"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    challenge_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("challenges.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    system_prompt_ciphertext: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )  # Encrypted at rest via AES-256-GCM (Rule §2)
    system_prompt_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )  # SHA-256 hash for integrity verification
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships — lazy="raise" prevents un-eager-loaded queries
    challenge = relationship("Challenge", back_populates="versions", lazy="raise")
    bindings = relationship(
        "ChallengeModelBinding",
        back_populates="challenge_version",
        cascade="all, delete-orphan",
        lazy="raise",
    )
