"""Common API schemas: error envelopes and pagination cursors."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """Standardized error envelope payload."""

    code: str = Field(description="Machine-readable error code")
    message: str = Field(description="Human-readable error explanation")


class ErrorResponse(BaseModel):
    """Top-level error response model."""

    error: ErrorDetail


class PaginationCursor(BaseModel):
    """Cursor metadata for keyset pagination."""

    created_at: datetime
    id: uuid.UUID
