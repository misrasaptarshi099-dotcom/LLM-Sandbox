"""Public challenge schemas.

Rules §2, PRD §8:
- System prompt ciphertext and plaintext secrets are strictly omitted from all public schemas.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ModelPublicInfo(BaseModel):
    """Allowed model configuration for a challenge."""

    model_name: str = Field(description="Name of the permitted model")
    max_input_tokens: int = Field(description="Maximum allowed input tokens")
    max_output_tokens: int = Field(description="Maximum allowed output tokens")
    temperature: float = Field(description="Configured sampling temperature")


class ChallengePublicResponse(BaseModel):
    """Public challenge details."""

    slug: str = Field(description="Unique slug identifier of the challenge")
    title: str = Field(description="Human-readable challenge title")
    status: str = Field(description="Current challenge status (e.g. LIVE)")
    latest_version: int = Field(description="Active published version number")
    allowed_models: list[ModelPublicInfo] = Field(
        default_factory=list,
        description="List of permitted model configurations",
    )


class ChallengeListResponse(BaseModel):
    """List of active public challenges."""

    challenges: list[ChallengePublicResponse]
