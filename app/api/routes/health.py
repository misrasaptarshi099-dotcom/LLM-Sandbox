"""Health check endpoints.

Rule §10, Architecture §15:
- /health/live: Shallow liveness — does not call the LLM or spend money.
- /health/ready: Readiness probe (shallow process readiness in initial phase;
  deep DB/Redis connectivity check integrated as services activate).
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """Shallow liveness probe — always succeeds if the process is up."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness() -> dict[str, str]:
    """Readiness probe — verifies the process can serve traffic."""
    return {"status": "ok"}
