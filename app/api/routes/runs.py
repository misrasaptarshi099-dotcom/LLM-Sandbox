"""Run submission, status polling, and execution history endpoints.

Rules §4, §5, §10, PRD §4, Architecture §5:
- POST /v1/runs returns 202 Accepted and never waits for LLM generation.
- GET /v1/runs/{run_id} performs single-query status lookup.
- GET /v1/runs uses deterministic keyset pagination.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_admission_service, get_current_user
from app.db.models.user import User
from app.db.repositories.runs import RunRepository
from app.db.session import get_db_session
from app.schemas.runs import (
    RunCreateRequest,
    RunCreateResponse,
    RunResultResponse,
    RunStatusResponse,
    UserHistoryResponse,
)
from app.services.admission import AdmissionService

router = APIRouter(prefix="/v1/runs", tags=["runs"])


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunCreateResponse,
    summary="Submit a prompt injection attempt",
)
async def submit_run(
    request: RunCreateRequest,
    raw_request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    admission_service: Annotated[AdmissionService, Depends(get_admission_service)],
) -> RunCreateResponse:
    """Submit a prompt against a challenge.

    Returns 202 Accepted with a queued run identifier immediately.
    Never blocks on model generation.
    """
    client_ip = raw_request.client.host if raw_request.client else None
    return await admission_service.admit_run(
        user_id=current_user.id,
        request=request,
        client_ip=client_ip,
    )


@router.get(
    "/{run_id}",
    response_model=RunStatusResponse,
    summary="Get status and telemetry of a run",
)
async def get_run_status(
    run_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunStatusResponse:
    """Hot status query: 1 primary-key lookup + 1-to-1 join (Rule §5)."""
    repo = RunRepository(session)
    run = await repo.get_run_with_result(run_id=run_id, user_id=current_user.id)
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run '{run_id}' not found.",
        )

    result_payload = None
    if run.result:
        result_payload = RunResultResponse(
            response_preview=run.result.response_preview,
            input_tokens=run.result.input_tokens,
            output_tokens=run.result.output_tokens,
            duration_ms=run.result.duration_ms,
            finish_reason=run.result.finish_reason,
        )

    return RunStatusResponse(
        id=run.id,
        status=run.status,
        prompt_bytes=run.prompt_bytes,
        attempt_count=run.attempt_count,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        result=result_payload,
    )


@router.get(
    "",
    response_model=UserHistoryResponse,
    summary="Get user run history with keyset pagination",
)
async def get_user_runs(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    cursor: Annotated[
        str | None, Query(description="Opaque cursor token from previous page")
    ] = None,
    cursor_created_at: Annotated[datetime | None, Query()] = None,
    cursor_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> UserHistoryResponse:
    """Keyset pagination for user runs history avoiding deep offset scans."""
    parsed_created_at = cursor_created_at
    parsed_id = cursor_id

    if cursor:
        try:
            parts = cursor.split(",", 1)
            if len(parts) != 2:
                raise ValueError("Invalid cursor format")
            parsed_created_at = datetime.fromisoformat(parts[0])
            parsed_id = uuid.UUID(parts[1])
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Invalid pagination cursor: {exc}",
            ) from exc
    elif (cursor_created_at is None) ^ (cursor_id is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Both cursor_created_at and cursor_id must be provided together.",
        )

    repo = RunRepository(session)
    runs = await repo.get_user_history(
        user_id=current_user.id,
        cursor_created_at=parsed_created_at,
        cursor_id=parsed_id,
        limit=limit,
    )

    items: list[RunStatusResponse] = []
    for r in runs:
        res = None
        if r.result:
            res = RunResultResponse(
                response_preview=r.result.response_preview,
                input_tokens=r.result.input_tokens,
                output_tokens=r.result.output_tokens,
                duration_ms=r.result.duration_ms,
                finish_reason=r.result.finish_reason,
            )
        items.append(
            RunStatusResponse(
                id=r.id,
                status=r.status,
                prompt_bytes=r.prompt_bytes,
                attempt_count=r.attempt_count,
                created_at=r.created_at,
                started_at=r.started_at,
                finished_at=r.finished_at,
                result=res,
            )
        )

    next_cursor = None
    if len(items) == limit:
        last_item = items[-1]
        next_cursor = f"{last_item.created_at.isoformat()},{last_item.id}"

    return UserHistoryResponse(runs=items, next_cursor=next_cursor)
