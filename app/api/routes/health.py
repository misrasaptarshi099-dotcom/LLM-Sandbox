"""Health check endpoints.

Rule §10, Architecture §15:
- /health/live: Shallow liveness — does not call the LLM or spend money.
- /health/ready: Verifies dependency connectivity (DB, Redis)
  without generating model traffic.
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
    """Readiness probe — verifies the process can serve traffic.

    In Phase 1 this is a shallow check. Phase 2 will add DB/Redis
    connectivity verification.
    """
    return {"status": "ok"}
