"""Run model — represents a participant's execution attempt against a challenge.

BCNF Functional Dependencies:
id -> user_id, model_binding_id, status, prompt_hash, prompt_bytes,
      attempt_count, created_at, started_at, finished_at

Candidate key: id (PK).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'COMPLETED', "
            "'PROVIDER_ERROR', 'TIMEOUT', 'ADMISSION_REJECTED', 'SYSTEM_ERROR')",
            name="ck_runs_status",
        ),
        CheckConstraint("prompt_bytes >= 0", name="ck_runs_prompt_bytes"),
        CheckConstraint("attempt_count >= 0", name="ck_runs_attempt_count"),
        # Keyset pagination index: filtering by user_id and ordering by (created_at DESC, id DESC)
        Index("idx_runs_user_created_id", "user_id", "created_at", "id"),
        # Hot queue index: worker polling and monitoring
        Index("idx_runs_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_binding_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("challenge_model_bindings.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(24),
        default="QUEUED",
        nullable=False,
    )
    prompt_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )  # SHA-256 hash of participant prompt
    prompt_bytes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationships
    user = relationship("User", back_populates="runs")
    model_binding = relationship("ChallengeModelBinding", back_populates="runs")
    result = relationship(
        "RunResult",
        back_populates="run",
        uselist=False,
        cascade="all, delete-orphan",
    )
