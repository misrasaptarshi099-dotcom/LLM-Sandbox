"""Integration tests for query-count regression and N+1 query prevention.

Rules §5, Architecture §10:
- Hot status queries (GET /v1/runs/{id}) must execute in exactly 1 query with 1-to-1 join.
- History queries (GET /v1/runs) must execute in O(1) queries (1 for runs + 1 for selectinload)
  regardless of the number of items returned (N+1 immunity).
- Challenge list queries must execute in O(1) queries (selectinload chained) regardless of
  the number of challenges.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.deps import get_current_user
from app.db.models.challenge import Challenge
from app.db.models.challenge_model_binding import ChallengeModelBinding
from app.db.models.challenge_version import ChallengeVersion
from app.db.models.model import Model
from app.db.models.run import Run
from app.db.models.run_result import RunResult
from app.main import app


@contextmanager
def capture_queries(engine: AsyncEngine) -> Iterator[list[str]]:
    """Context manager to intercept and record SQL statements executed on the engine."""
    statements: list[str] = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        clean = statement.strip().upper()
        # Filter to DML and query statements (exclude internal transaction savepoints / pragmas)
        if any(clean.startswith(prefix) for prefix in ("SELECT", "INSERT", "UPDATE", "DELETE")):
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before_cursor_execute)


@pytest.mark.asyncio
async def test_get_run_status_executes_single_query(
    client: AsyncClient,
    seeded_test_env: dict,
    test_db_session: AsyncSession,
) -> None:
    """Hot status query must execute in exactly 1 query with joinedload (Rule §5)."""
    user = seeded_test_env["user"]
    binding = seeded_test_env["binding"]
    engine = test_db_session.bind
    app.dependency_overrides[get_current_user] = lambda: user

    # Insert run with result
    run = Run(
        user_id=user.id,
        model_binding_id=binding.id,
        status="COMPLETED",
        prompt_hash="dummy_hash_123",
        prompt_bytes=10,
        prompt_ciphertext="dummy_ciphertext",
    )
    test_db_session.add(run)
    await test_db_session.flush()

    result = RunResult(
        run_id=run.id,
        response_preview="Hello world",
        input_tokens=10,
        output_tokens=5,
        duration_ms=120,
    )
    test_db_session.add(result)
    await test_db_session.commit()

    with capture_queries(engine) as captured:
        response = await client.get(f"/v1/runs/{run.id}")
        assert response.status_code == 200

    # Must be 1 query: SELECT runs ... LEFT OUTER JOIN run_results ...
    select_queries = [q for q in captured if q.strip().upper().startswith("SELECT")]
    assert len(select_queries) == 1, (
        f"Expected exactly 1 SQL query for GET /v1/runs/{{id}}, got {len(select_queries)}: "
        f"{select_queries}"
    )


@pytest.mark.asyncio
async def test_get_user_runs_history_n_plus_one_immunity(
    client: AsyncClient,
    seeded_test_env: dict,
    test_db_session: AsyncSession,
) -> None:
    """GET /v1/runs query count must remain constant regardless of result count (no N+1)."""
    user = seeded_test_env["user"]
    binding = seeded_test_env["binding"]
    engine = test_db_session.bind
    app.dependency_overrides[get_current_user] = lambda: user

    # 1. Seed 3 runs with results
    for i in range(3):
        r = Run(
            user_id=user.id,
            model_binding_id=binding.id,
            status="COMPLETED",
            prompt_hash=f"hash_{i}_{uuid.uuid4().hex[:6]}",
            prompt_bytes=10,
            prompt_ciphertext="dummy_ciphertext",
        )
        test_db_session.add(r)
        await test_db_session.flush()
        test_db_session.add(RunResult(run_id=r.id, response_preview=f"Result {i}"))
    await test_db_session.commit()

    with capture_queries(engine) as captured_3_items:
        res = await client.get("/v1/runs?limit=10")
        assert res.status_code == 200
        assert len(res.json()["runs"]) >= 3

    selects_3 = [q for q in captured_3_items if q.strip().upper().startswith("SELECT")]

    # 2. Seed 10 more runs with results (total 13+ runs)
    for i in range(10):
        r = Run(
            user_id=user.id,
            model_binding_id=binding.id,
            status="COMPLETED",
            prompt_hash=f"hash_more_{i}_{uuid.uuid4().hex[:6]}",
            prompt_bytes=10,
            prompt_ciphertext="dummy_ciphertext",
        )
        test_db_session.add(r)
        await test_db_session.flush()
        test_db_session.add(RunResult(run_id=r.id, response_preview=f"Result more {i}"))
    await test_db_session.commit()

    with capture_queries(engine) as captured_13_items:
        res = await client.get("/v1/runs?limit=50")
        assert res.status_code == 200
        assert len(res.json()["runs"]) >= 13

    selects_13 = [q for q in captured_13_items if q.strip().upper().startswith("SELECT")]

    # Query counts must be identical: 2 queries (1 select runs, 1 selectinload run_results)
    assert len(selects_3) == len(selects_13), (
        f"N+1 regression detected! 3 items took {len(selects_3)} queries, "
        f"while 13 items took {len(selects_13)} queries."
    )
    assert len(selects_13) == 2, (
        f"Expected exactly 2 queries for history (runs + selectinload), got {len(selects_13)}"
    )


@pytest.mark.asyncio
async def test_list_challenges_n_plus_one_immunity(
    client: AsyncClient,
    seeded_test_env: dict,
    test_db_session: AsyncSession,
) -> None:
    """GET /v1/challenges query count must remain constant as challenges scale."""
    provider = seeded_test_env["provider"]
    engine = test_db_session.bind

    # Initial query count with 1 challenge
    with capture_queries(engine) as captured_1:
        res1 = await client.get("/v1/challenges")
        assert res1.status_code == 200

    selects_1 = [q for q in captured_1 if q.strip().upper().startswith("SELECT")]

    # Add 2 more challenges with versions, models, and bindings
    for i in range(2):
        ch = Challenge(slug=f"perf-challenge-{i}", title=f"Perf Challenge {i}", status="LIVE")
        test_db_session.add(ch)
        await test_db_session.flush()

        ver = ChallengeVersion(
            challenge_id=ch.id,
            version_no=1,
            system_prompt_ciphertext="dummy_ciphertext",
            system_prompt_hash="dummy_hash",
            published_at=ch.created_at,
        )
        test_db_session.add(ver)
        await test_db_session.flush()

        m = Model(provider_id=provider.id, model_name=f"perf-model-{i}", active=True)
        test_db_session.add(m)
        await test_db_session.flush()

        binding = ChallengeModelBinding(
            challenge_version_id=ver.id,
            model_id=m.id,
            active=True,
        )
        test_db_session.add(binding)
    await test_db_session.commit()

    with capture_queries(engine) as captured_3:
        res2 = await client.get("/v1/challenges")
        assert res2.status_code == 200
        assert len(res2.json()["challenges"]) >= 3

    selects_3 = [q for q in captured_3 if q.strip().upper().startswith("SELECT")]

    # Query counts must be identical (O(1) via selectinload)
    assert len(selects_1) == len(selects_3), (
        f"N+1 regression in challenge listing! 1 challenge took {len(selects_1)} queries, "
        f"3 challenges took {len(selects_3)} queries."
    )
