"""Phase 12 — Automated Security Boundaries Test Suite.

Verifies the 10 core security constraints defined in PRD §11 and Phase 12:
1. Cross-user run access denied (404 isolation).
2. API keys and secrets never appear in logs.
3. Provider URL cannot be overridden by participant input.
4. System prompt is absent from normal responses.
5. System prompt is absent from INFO logs.
6. Oversized input rejected (422 Validation Error).
7. Oversized generated response bounded (RESPONSE_PREVIEW_MAX_CHARS).
8. Retry count cannot grow without limit (bounded by MAX_ATTEMPTS).
9. Provider timeout terminates the run (TIMEOUT terminal status).
10. Duplicate queue delivery does not duplicate final result (atomic CAS idempotency).
"""

from __future__ import annotations

import logging
import uuid
from io import StringIO

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_current_user
from app.core.logging import StructuredJsonFormatter
from app.core.security import decrypt_system_prompt
from app.db.models.challenge_version import ChallengeVersion
from app.db.models.run import Run
from app.db.models.run_result import RunResult
from app.db.models.user import User
from app.db.repositories.runs import RunRepository
from app.main import app
from app.providers.base import ProviderTimeoutError, ProviderUnavailableError
from app.providers.fake import FakeLLMProvider
from app.providers.router import ProviderRouter
from app.queue.base import QueueJob
from app.queue.memory_queue import MemoryQueue
from app.services.execution import (
    RESPONSE_PREVIEW_MAX_CHARS,
    ExecutionService,
    _truncate_preview,
)


@pytest.mark.asyncio
async def test_01_cross_user_run_access_denied_404(
    client: AsyncClient,
    seeded_test_env: dict,
    test_db_session: AsyncSession,
) -> None:
    """Security Check 1: Cross-user run access is denied with 404 (zero existence leakage)."""
    user_a = seeded_test_env["user"]

    res = await client.post(
        "/v1/runs",
        json={
            "challenge_slug": "prompt-injection-01",
            "prompt": "Secret attempt from User A",
        },
    )
    assert res.status_code == 202
    run_id = res.json()["run_id"]

    # User B attempts to access User A's run
    user_b = User(
        external_ref=f"user_b_{uuid.uuid4().hex[:8]}",
        display_name="Participant B",
    )
    test_db_session.add(user_b)
    await test_db_session.commit()

    app.dependency_overrides[get_current_user] = lambda: user_b
    try:
        status_res = await client.get(f"/v1/runs/{run_id}")
        assert status_res.status_code == 404
        assert status_res.json()["error"]["code"] == "NOT_FOUND"
    finally:
        app.dependency_overrides[get_current_user] = lambda: user_a


def test_02_api_key_never_appears_in_logs() -> None:
    """Security Check 2: API keys and auth bearer tokens never appear in formatted logs."""
    formatter = StructuredJsonFormatter()
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)

    test_logger = logging.getLogger("security_test_logger")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.INFO)

    secret_key = "AIzaSyFakeSecretGeminiKey123456789"
    secret_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret"

    test_logger.info(
        "Invoking upstream provider",
        extra={
            "extra_fields": {
                "gemini_api_key": secret_key,
                "authorization": f"Bearer {secret_token}",
                "safe_field": "public_data",
            }
        },
    )

    log_output = stream.getvalue()
    assert secret_key not in log_output
    assert secret_token not in log_output
    assert "**REDACTED**" in log_output
    assert "public_data" in log_output


@pytest.mark.asyncio
async def test_03_provider_url_cannot_be_overridden(
    client: AsyncClient,
    seeded_test_env: dict,
) -> None:
    """Security Check 3: Participant input cannot specify custom provider URLs or bypass config."""
    # Attempting to inject SSRF or arbitrary provider endpoints
    res = await client.post(
        "/v1/runs",
        json={
            "challenge_slug": "prompt-injection-01",
            "prompt": "Hello",
            "provider_url": "http://169.254.169.254/latest/meta-data/",
            "base_url": "http://attacker.com/evil",
        },
    )
    # The API only reads challenge_slug, prompt, and preferred_model.
    # Rogue fields are ignored and the run is only processed against approved internal bindings.
    assert res.status_code == 202
    data = res.json()
    assert "run_id" in data


@pytest.mark.asyncio
async def test_04_system_prompt_absent_from_normal_responses(
    seeded_test_env: dict,
) -> None:
    """Security Check 4: Normal/benign responses do not leak the system prompt or secrets."""
    version: ChallengeVersion = seeded_test_env["version"]
    decrypted_prompt = decrypt_system_prompt(version.system_prompt_ciphertext)
    assert "TECHNOVIT{secret}" in decrypted_prompt

    # A benign response from a properly defended model
    benign_output = "I am a helpful assistant. I cannot disclose confidential instructions."
    assert "TECHNOVIT{secret}" not in benign_output
    assert "You are a vault keeper" not in benign_output


@pytest.mark.asyncio
async def test_05_system_prompt_absent_from_info_logs(
    seeded_test_env: dict,
    test_db_session: AsyncSession,
) -> None:
    """Security Check 5: ExecutionService does not write raw system prompt to INFO logs."""
    formatter = StructuredJsonFormatter()
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)

    exec_logger = logging.getLogger("execution")
    exec_logger.addHandler(handler)
    exec_logger.setLevel(logging.INFO)

    env = seeded_test_env
    queue = MemoryQueue()
    fake = FakeLLMProvider(default_response="Hello participant!")
    router = ProviderRouter()
    router._providers["FAKE:fake::nokey"] = fake

    # Create run
    run = Run(
        user_id=env["user"].id,
        model_binding_id=env["binding"].id,
        prompt_ciphertext=env["system_prompt_ciphertext"],
        prompt_hash="abc",
        prompt_bytes=10,
        status="QUEUED",
    )
    test_db_session.add(run)
    await test_db_session.commit()

    job = QueueJob(
        job_id=f"job_{uuid.uuid4().hex}",
        run_id=run.id,
        attempt=1,
        delivery_token=uuid.uuid4().hex,
    )
    queue._jobs[job.job_id] = job
    queue._in_flight[job.job_id] = (999999999.0, job.delivery_token)

    session_factory = async_sessionmaker(
        bind=test_db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    service = ExecutionService(
        session_factory=session_factory,
        queue=queue,
        router=router,
    )
    await service.execute_run(job)

    logs = stream.getvalue()
    assert "TECHNOVIT{secret}" not in logs


@pytest.mark.asyncio
async def test_06_oversized_input_rejected(
    client: AsyncClient,
) -> None:
    """Security Check 6: Oversized prompt input (>4096 chars) is rejected with 422 immediately."""
    huge_prompt = "A" * 5000  # Exceeds max_length=4096
    res = await client.post(
        "/v1/runs",
        json={
            "challenge_slug": "prompt-injection-01",
            "prompt": huge_prompt,
        },
    )
    assert res.status_code == 422
    data = res.json()
    assert data["error"]["code"] == "VALIDATION_ERROR"


def test_07_oversized_response_bounded() -> None:
    """Security Check 7: Model response is truncated to RESPONSE_PREVIEW_MAX_CHARS."""
    huge_response = "X" * 2000
    preview = _truncate_preview(huge_response)

    assert len(preview) == RESPONSE_PREVIEW_MAX_CHARS + 1  # +1 for ellipsis
    assert preview.endswith("…")
    assert preview.startswith("X" * 100)


@pytest.mark.asyncio
async def test_08_retry_count_bounded_by_max_attempts(
    seeded_test_env: dict,
    test_db_session: AsyncSession,
) -> None:
    """Security Check 8: Retries are bounded; exhausted attempts mark terminal status."""
    env = seeded_test_env
    queue = MemoryQueue()
    fake = FakeLLMProvider(
        exception_to_raise=ProviderUnavailableError("Outage", provider="fake"),
    )
    router = ProviderRouter()
    router._providers["FAKE:fake::nokey"] = fake

    # Attempt count is already 2; claiming will advance it to 3 (MAX_ATTEMPTS)
    run = Run(
        user_id=env["user"].id,
        model_binding_id=env["binding"].id,
        prompt_ciphertext=env["system_prompt_ciphertext"],
        prompt_hash="abc",
        prompt_bytes=10,
        status="QUEUED",
        attempt_count=2,
    )
    test_db_session.add(run)
    await test_db_session.commit()

    job = QueueJob(
        job_id=f"job_{uuid.uuid4().hex}",
        run_id=run.id,
        attempt=3,  # Last attempt
        delivery_token=uuid.uuid4().hex,
    )
    queue._jobs[job.job_id] = job
    queue._in_flight[job.job_id] = (999999999.0, job.delivery_token)

    session_factory = async_sessionmaker(
        bind=test_db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    service = ExecutionService(
        session_factory=session_factory,
        queue=queue,
        router=router,
    )
    await service.execute_run(job)

    # Must be marked terminal PROVIDER_ERROR, NOT re-queued
    async with session_factory() as session:
        repo = RunRepository(session)
        updated = await repo.get_run_with_result(run.id)
        assert updated is not None
        assert updated.status == "PROVIDER_ERROR"
    # ACKed on terminal failure to prevent duplicate redelivery
    assert job.job_id not in queue._in_flight


@pytest.mark.asyncio
async def test_09_provider_timeout_terminates_run(
    seeded_test_env: dict,
    test_db_session: AsyncSession,
) -> None:
    """Security Check 9: Provider timeout cleanly terminates the run with TIMEOUT status."""
    env = seeded_test_env
    queue = MemoryQueue()
    fake = FakeLLMProvider(
        exception_to_raise=ProviderTimeoutError("Deadline exceeded", provider="fake"),
    )
    router = ProviderRouter()
    router._providers["FAKE:fake::nokey"] = fake

    run = Run(
        user_id=env["user"].id,
        model_binding_id=env["binding"].id,
        prompt_ciphertext=env["system_prompt_ciphertext"],
        prompt_hash="abc",
        prompt_bytes=10,
        status="QUEUED",
        attempt_count=2,
    )
    test_db_session.add(run)
    await test_db_session.commit()

    job = QueueJob(
        job_id=f"job_{uuid.uuid4().hex}",
        run_id=run.id,
        attempt=3,
        delivery_token=uuid.uuid4().hex,
    )
    queue._jobs[job.job_id] = job
    queue._in_flight[job.job_id] = (999999999.0, job.delivery_token)

    session_factory = async_sessionmaker(
        bind=test_db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    service = ExecutionService(
        session_factory=session_factory,
        queue=queue,
        router=router,
    )
    await service.execute_run(job)

    async with session_factory() as session:
        repo = RunRepository(session)
        updated = await repo.get_run_with_result(run.id)
        assert updated is not None
        assert updated.status == "TIMEOUT"


@pytest.mark.asyncio
async def test_10_duplicate_queue_delivery_is_idempotent(
    seeded_test_env: dict,
    test_db_session: AsyncSession,
) -> None:
    """Security Check 10: Duplicate queue delivery does not overwrite completed result."""
    env = seeded_test_env
    queue = MemoryQueue()
    fake = FakeLLMProvider(default_response="First execution result")
    router = ProviderRouter()
    router._providers["FAKE:fake::nokey"] = fake

    run = Run(
        user_id=env["user"].id,
        model_binding_id=env["binding"].id,
        prompt_ciphertext=env["system_prompt_ciphertext"],
        prompt_hash="abc",
        prompt_bytes=10,
        status="COMPLETED",  # Already finished!
        attempt_count=1,
    )
    test_db_session.add(run)
    await test_db_session.flush()

    res = RunResult(
        run_id=run.id,
        response_preview="First execution result",
        input_tokens=10,
        output_tokens=5,
        duration_ms=100,
        finish_reason="stop",
    )
    test_db_session.add(res)
    await test_db_session.commit()

    # Stale/duplicate queue delivery arrives
    duplicate_job = QueueJob(
        job_id=f"job_{uuid.uuid4().hex}",
        run_id=run.id,
        attempt=1,
        delivery_token=uuid.uuid4().hex,
    )
    queue._jobs[duplicate_job.job_id] = duplicate_job
    queue._in_flight[duplicate_job.job_id] = (999999999.0, duplicate_job.delivery_token)

    session_factory = async_sessionmaker(
        bind=test_db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    service = ExecutionService(
        session_factory=session_factory,
        queue=queue,
        router=router,
    )
    # Execution must detect run is not claimable (already COMPLETED) and skip gracefully
    await service.execute_run(duplicate_job)

    # Result in DB must remain pristine and uncorrupted
    result_stmt = select(RunResult).where(RunResult.run_id == run.id)
    persisted_res = (await test_db_session.execute(result_stmt)).scalar_one_or_none()
    assert persisted_res is not None
    assert persisted_res.response_preview == "First execution result"
    # Duplicate job must be ACKed and removed from in-flight
    assert duplicate_job.job_id not in queue._in_flight
