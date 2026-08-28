"""RunResult model — represents execution output and token telemetry for a completed run.

BCNF Functional Dependencies:
run_id -> response_object_key, response_preview, input_tokens, output_tokens,
          duration_ms, finish_reason, provider_request_id, created_at

Candidate key: run_id (PK, FK).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class RunResult(Base):
    __tablename__ = "run_results"

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    response_object_key: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
    )  # Optional S3/blob pointer for large transcripts
    response_preview: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )  # Compact bounded model response preview
    input_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    output_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    duration_ms: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    finish_reason: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    provider_request_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Relationships
    run = relationship("Run", back_populates="result")
