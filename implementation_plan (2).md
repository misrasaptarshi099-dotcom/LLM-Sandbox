# Implementation Plan — LLM Sandbox

## 0. Scope

Backend first. Frontend is explicitly deferred.

The backend must be demonstrable using HTTP clients, CLI commands, unit/integration tests and load tests.

## Phase 1 — Project Bootstrap

### Tasks
- [ ] Add `pyproject.toml`.
- [ ] Set Python version and dependency constraints.
- [ ] Add FastAPI application.
- [ ] Add SQLAlchemy + PostgreSQL.
- [ ] Add Alembic.
- [ ] Add Redis client.
- [ ] Add httpx.
- [ ] Add pytest.
- [ ] Add structured logging.
- [ ] Add `.env.example`.

### Done when
- API starts.
- `/health/live` returns 200.
- Test suite can run with an empty database.

## Phase 2 — Database and BCNF Schema

### Tasks
- [ ] Implement users.
- [ ] Implement providers.
- [ ] Implement models.
- [ ] Implement challenges.
- [ ] Implement challenge versions.
- [ ] Implement challenge-model bindings.
- [ ] Implement runs.
- [ ] Implement run results.
- [ ] Add all candidate-key constraints.
- [ ] Add foreign keys and checks.
- [ ] Add required indexes.
- [ ] Add keyset pagination index.
- [ ] Add migration scripts.
- [ ] Add seed data.

### Verification
- [ ] Confirm no non-key determinant violates BCNF.
- [ ] Run `EXPLAIN (ANALYZE, BUFFERS)` on hot queries.
- [ ] Confirm status lookup is indexed.
- [ ] Confirm run history uses keyset pagination.

## Phase 3 — API Contract

### Endpoints
- [ ] `POST /v1/runs`
- [ ] `GET /v1/runs/{run_id}`
- [ ] `GET /v1/challenges`
- [ ] `GET /health/live`
- [ ] `GET /health/ready`

### Tasks
- [ ] Add Pydantic request/response schemas.
- [ ] Validate challenge ID.
- [ ] Validate prompt byte/character limits.
- [ ] Validate authenticated user.
- [ ] Add rate limiting.
- [ ] Add admission control.
- [ ] Create run row.
- [ ] Enqueue `run_id`.
- [ ] Return 202.

### Performance rule
The API route must never wait for model generation on the normal asynchronous path.

## Phase 4 — Queue

### Tasks
- [ ] Create Redis queue/stream abstraction.
- [ ] Define small job payload.
- [ ] Add consumer group or equivalent reliable-consumer mechanism.
- [ ] Add visibility/reclaim policy.
- [ ] Add retry metadata.
- [ ] Add dead-letter handling.
- [ ] Add queue depth metrics.

### Job payload

```json
{
  "run_id": "01J...",
  "attempt": 1
}
```

Do not put prompts, system prompts or credentials in Redis.

## Phase 5 — Provider Abstraction

### Tasks
- [ ] Create `LLMProvider` protocol.
- [ ] Create normalized request/response models.
- [ ] Implement Gemini-compatible HTTP adapter.
- [ ] Implement self-hosted HTTP adapter.
- [ ] Implement optional participant-credential path.
- [ ] Add provider routing.
- [ ] Add model allowlist.
- [ ] Add timeouts.

### Provider safety
- [ ] Allow only configured provider base URLs.
- [ ] Never accept arbitrary participant-supplied callback URLs.
- [ ] Never log provider Authorization headers.

## Phase 6 — Challenge Execution

### Tasks
- [ ] Load immutable challenge version.
- [ ] Decrypt system prompt only inside worker execution.
- [ ] Construct `SYSTEM + USER` request.
- [ ] Enforce input token/size budget.
- [ ] Enforce output token budget.
- [ ] Invoke model.
- [ ] Normalize provider response.
- [ ] Apply bounded retries.
- [ ] Persist final status and usage.
- [ ] Clear transient plaintext prompt/config where practical.

### Important design rule

Do not add a giant prompt-injection "firewall" that defeats the purpose of the round.

The challenge is the LLM behavior; the security boundary is the backend/infrastructure surrounding it.

## Phase 7 — Rate Limiting and Cost Guardrails

### Tasks
- [ ] Add per-user request rate limit.
- [ ] Add per-IP rate limit.
- [ ] Add global admission threshold.
- [ ] Add provider/model concurrency limit.
- [ ] Add max input tokens.
- [ ] Add max output tokens.
- [ ] Add event-wide budget configuration.
- [ ] Add circuit breaker.

### Cost-control order

```text
reject impossible requests
        ↓
rate-limit
        ↓
admission control
        ↓
token budget
        ↓
provider call
        ↓
bounded retry
```

Never use retry logic to compensate for bad admission control.

## Phase 8 — Security Hardening

### Tasks
- [ ] Secret-manager integration.
- [ ] Redacted structured logs.
- [ ] System prompt encryption at rest.
- [ ] No raw prompt in ordinary logs.
- [ ] No raw API keys in logs.
- [ ] Cross-user authorization tests.
- [ ] Provider URL allowlist.
- [ ] Request size limits.
- [ ] Response size limits.
- [ ] Dependency auditing.
- [ ] Container runs as non-root.
- [ ] Minimal runtime image.

## Phase 9 — Query and Code Quality

### Tasks
- [ ] Enforce route/service/repository separation.
- [ ] Add repository interfaces where helpful.
- [ ] Prevent ORM lazy loading on hot paths.
- [ ] Add query-count regression tests.
- [ ] Add mypy/pyright or equivalent static checks.
- [ ] Add Ruff/formatter/linting.
- [ ] Add pre-commit hooks if useful.
- [ ] Ban wildcard imports.
- [ ] Require explicit timeouts on network calls.
- [ ] Require explicit error classification.

## Phase 10 — Testing Without Frontend

### 10.1 Start infrastructure

```bash
docker compose up -d postgres redis
```

### 10.2 Run migrations

```bash
alembic upgrade head
```

### 10.3 Seed a challenge

```bash
python scripts/seed_challenge.py
```

### 10.4 Start API

```bash
uvicorn app.main:app --reload
```

### 10.5 Start worker

```bash
python -m app.workers.runner
```

### 10.6 Submit a run

```bash
curl -X POST http://localhost:8000/v1/runs \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dev-token" \
  -d '{
    "challenge_id": "prompt-injection-01",
    "prompt": "hello model"
  }'
```

Expected:

```json
{
  "run_id": "...",
  "status": "QUEUED"
}
```

### 10.7 Check the result

```bash
curl \
  -H "Authorization: Bearer dev-token" \
  http://localhost:8000/v1/runs/<RUN_ID>
```

Repeat until `COMPLETED` or a terminal failure.

### 10.8 Health

```bash
curl http://localhost:8000/health/live
curl http://localhost:8000/health/ready
```

## Phase 11 — Fake Provider for Deterministic Local Tests

Do not depend on a paid LLM for the entire test suite.

Implement a fake provider:

```text
FakeLLMProvider
├── fixed success
├── timeout
├── 429
├── 500
├── malformed response
└── deterministic token usage
```

This gives fast repeatable integration tests without spending money.

## Phase 12 — Security Tests

### Tests
- [ ] Cross-user run access denied.
- [ ] API key never appears in logs.
- [ ] Provider URL cannot be overridden by participant input.
- [ ] System prompt is absent from normal responses.
- [ ] System prompt is absent from INFO logs.
- [ ] Oversized input rejected.
- [ ] Oversized generated response bounded.
- [ ] Retry count cannot grow without limit.
- [ ] Provider timeout terminates the run.
- [ ] Duplicate queue delivery does not duplicate final result.

## Phase 13 — Load Tests

Use a CLI load tool such as k6, Locust or another HTTP load generator.

Measure:
- requests/sec;
- accepted/sec;
- queue wait p50/p95/p99;
- model latency p50/p95/p99;
- worker concurrency;
- DB CPU;
- DB connections;
- Redis memory;
- provider error rate;
- token usage;
- cost per 1,000 runs.

Test profiles:

```text
A. 1 user / 10 runs
B. 50 users / burst
C. 200 users / sustained burst
D. provider latency spike
E. provider 429 storm
F. worker restart during execution
```

## Phase 14 — Scalability Tuning

Tune in this order:

```text
1. token limits
2. rate limits
3. worker concurrency
4. provider connection pooling
5. PostgreSQL pool size
6. Redis throughput
7. API replica count
8. worker replica count
```

Do not scale infrastructure before confirming which resource is actually saturated.

## Phase 15 — Cloud Deployment

### Initial deployment
Use the simplest managed services available:
- managed PostgreSQL;
- managed Redis;
- containerized FastAPI API;
- containerized worker;
- secret manager;
- object storage if transcript retention is needed.

### Later scaling
- autoscale API separately from workers;
- autoscale workers from queue age/depth;
- use provider-specific worker pools where necessary.

Avoid introducing Kubernetes purely because "cloud project = Kubernetes". The rubric rewards good engineering, not YAML volume.

## Phase 16 — Final Demo Without Frontend

The backend demo should show:

```text
curl POST
   ↓
202 Accepted
   ↓
Redis queue
   ↓
Worker claims job
   ↓
LLM provider call
   ↓
PostgreSQL finalization
   ↓
curl GET
   ↓
Model response + token metrics
```

Then demonstrate:
- rate limiting;
- provider timeout;
- retry handling;
- duplicate job handling;
- query-count test;
- EXPLAIN plan;
- concurrent load.

## Definition of Done

- [ ] API works with curl.
- [ ] Worker executes jobs.
- [ ] Fake provider tests pass.
- [ ] Real provider integration works.
- [ ] System prompt is protected from backend leaks.
- [ ] Secrets are redacted.
- [ ] BCNF schema migrated.
- [ ] Hot queries are indexed.
- [ ] Keyset pagination implemented.
- [ ] N+1 regression tests pass.
- [ ] Rate limits work.
- [ ] Bounded retries work.
- [ ] Load test report exists.
- [ ] Cost per 1,000 runs is measured.
- [ ] Deployment is reproducible.
- [ ] Frontend is not required to demonstrate the backend.
