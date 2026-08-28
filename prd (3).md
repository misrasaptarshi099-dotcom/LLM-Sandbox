# PRD — LLM Sandbox

**Project:** TechnoVIT / GDG VIT Chennai — Prompt Injection Round  
**Version:** 1.0  
**Scope:** Backend-first implementation; frontend intentionally deferred  
**Primary Goal:** Build a secure, scalable, cost-efficient LLM challenge backend in which the round's solution is embedded in a system prompt and participants interact with the model through controlled user prompts.

## 1. Product Summary

The LLM Sandbox accepts a participant prompt, executes it against a versioned challenge configuration and an LLM provider, and returns the model response plus challenge-safe execution metadata.

The platform must support:
- self-hosted models;
- third-party LLM APIs;
- participant-supplied provider credentials when the event rules allow it;
- a provider/model abstraction so the core application is vendor-neutral.

The challenge is intentionally a prompt-injection exercise. The backend therefore **does not attempt to make the prompt impossible to attack**. Instead, it protects everything outside the intended game mechanic: infrastructure, credentials, databases, internal APIs, provider keys, filesystem, logs, and other participants.

## 2. Problem Statement

The event needs an LLM-backed challenge that can handle bursty traffic without:
- exposing provider credentials;
- leaking the system prompt through API errors, logs, debug endpoints or tracing;
- allowing one participant to affect another;
- turning rate spikes into uncontrolled LLM spend;
- creating retry storms;
- depending on a single LLM vendor;
- storing sensitive credentials or raw transcripts unnecessarily.

## 3. Goals

### Functional
- Accept participant prompts through an authenticated API.
- Resolve an immutable challenge version.
- Resolve a permitted model/provider configuration.
- Call the LLM with a controlled request envelope.
- Return the generated model response.
- Record usage and execution metadata.
- Support configurable model parameters such as max output tokens and temperature.
- Support challenge enable/disable and versioning.
- Support provider failover only when it does not change challenge semantics.

### Non-functional
- Stateless API tier.
- Horizontally scalable workers.
- Bounded concurrency.
- Per-user and global rate limits.
- Hard token budgets.
- Request and response size limits.
- Bounded timeouts.
- Idempotent run finalization.
- No N+1 query patterns.
- BCNF relational schema.
- Strong secret-management discipline.
- Reproducible cloud deployment.
- Measurable cost per 1,000 runs.

## 4. Non-Goals

- Building a full chat application.
- Long-lived conversation memory.
- General-purpose autonomous agents.
- Tool-calling that exposes host capabilities.
- Arbitrary browsing or internet access for models.
- Storing unlimited chat history.
- Supporting every LLM vendor on day one.
- Making the challenge permanently unbreakable.

## 5. Users

### Participant
Submits prompts and receives the model response and run status.

### Event Operator
Creates/version-controls challenge prompts, configures allowed models and limits, and observes aggregate health.

### Platform Operator
Operates API, worker, database, cache/queue, secrets and monitoring.

## 6. Core User Flow

1. Participant authenticates.
2. Participant submits a prompt with a challenge ID.
3. API validates size, challenge status, rate limit and admission capacity.
4. API creates one durable run record.
5. API enqueues one small job containing the run ID.
6. Worker loads the immutable challenge/model configuration.
7. Worker invokes the LLM through the provider abstraction.
8. Worker applies bounded retries only for transient provider failures.
9. Worker stores compact execution/usage metadata and the response according to retention policy.
10. Participant polls the run endpoint or receives a future push notification.

## 7. Challenge Model

A challenge consists of:
- challenge identity;
- immutable version;
- system prompt containing the challenge's intended solution;
- model/provider binding;
- generation limits;
- timeout;
- response-size limit;
- optional success detector for operator analytics.

The system prompt is the game secret. It must never be returned by an API endpoint, written to normal application logs, or included in generic error messages.

## 8. API Contract

### `POST /v1/runs`

Request:

```json
{
  "challenge_id": "prompt-injection-01",
  "prompt": "participant prompt here"
}
```

Response:

```json
{
  "run_id": "01J...",
  "status": "QUEUED"
}
```

### `GET /v1/runs/{run_id}`

Response:

```json
{
  "run_id": "01J...",
  "status": "COMPLETED",
  "response": "model response",
  "usage": {
    "input_tokens": 234,
    "output_tokens": 71
  },
  "duration_ms": 1830,
  "completed_at": "2026-08-27T18:00:00Z"
}
```

### `GET /v1/challenges`

Return only public challenge metadata. Never return the system prompt.

### Health
- `GET /health/live`
- `GET /health/ready`

## 9. Cost Controls

Every request must be cost-bounded before reaching the provider:
- input character/token ceiling;
- maximum output tokens;
- maximum concurrency per provider/model;
- per-user request rate;
- global request rate;
- optional event-wide budget ceiling;
- provider timeout;
- retry budget;
- circuit breaker;
- no retry for deterministic validation failures;
- no retry for authentication failures;
- no retry for model refusal/content-policy terminal outcomes unless explicitly required by the challenge.

Prefer a smaller model for local testing and development. Production model selection must be benchmarked against challenge behavior, quality and cost.

## 10. Scalability Requirements

The API must remain stateless and should not wait synchronously for model generation when load requires queueing.

Scaling signals:
- queue depth;
- oldest queue age;
- provider latency;
- worker utilization;
- rate-limit rejection rate;
- error rate.

Avoid scaling solely on HTTP request rate because LLM workloads vary dramatically in token usage and latency.

## 11. Security Requirements

### Secrets
- Provider credentials come from a secret manager or environment injection.
- Participant-provided credentials are ephemeral and never persisted by default.
- Never log `Authorization`, API keys, raw request headers or secret-bearing payloads.
- Never expose environment variables to the model.

### System prompt
- Never expose the raw system prompt to participants through API responses.
- Do not log prompts/responses at INFO level by default.
- Never place provider errors containing the raw request envelope directly into the client response.
- Use redacted structured logs.

### Prompt boundary
Treat participant prompt as untrusted data.
- Never interpolate participant text into shell commands.
- Never let participant text select provider endpoints, model names, database queries or filesystem paths directly.
- Never let participant text alter trusted backend policy.
- Keep challenge instructions separate from application/system configuration.

### Provider/network controls
- Only allow configured provider endpoints.
- Use explicit outbound allowlists.
- Disable arbitrary URL forwarding.
- Validate TLS.
- Apply short connection and read timeouts.

### Multi-user isolation
- A run may access only its own result.
- Challenge configuration is read-only during execution.
- No participant can select another participant's credentials, run ID or internal configuration.

## 12. Reliability

Terminal run outcomes:
- `COMPLETED`
- `PROVIDER_ERROR`
- `TIMEOUT`
- `RATE_LIMITED`
- `VALIDATION_ERROR`
- `ADMISSION_REJECTED`
- `SYSTEM_ERROR`

Retryable:
- transient network failure;
- provider 429/5xx when safe;
- worker crash before finalization.

Non-retryable:
- malformed input;
- invalid challenge;
- authentication failure;
- deterministic provider rejection;
- participant prompt exceeding limits.

Retries must be bounded and idempotent.

## 13. Observability

Metrics:
- runs accepted/sec;
- queue depth;
- queue age;
- LLM request latency p50/p95/p99;
- time to first token if streaming is later added;
- input/output tokens;
- provider error rate;
- retry count;
- timeout count;
- rate-limit count;
- cost per 1,000 runs;
- DB query latency;
- DB pool saturation;
- cache hit ratio.

Do not store raw system prompts or unrestricted prompt/response logs in telemetry.

## 14. Acceptance Criteria

- Participant prompts are processed through a configured LLM.
- Challenge system prompt is never exposed by normal API responses.
- Provider credentials are not present in logs.
- One normal run does not trigger per-field or per-token database queries.
- One status read is a bounded indexed lookup.
- LLM retries are bounded.
- API remains responsive while workers are busy.
- Excess load causes controlled admission rejection instead of unlimited spend.
- The same challenge version uses the same immutable configuration.
- Deployment is reproducible.
- The backend can be fully demonstrated without a frontend.
