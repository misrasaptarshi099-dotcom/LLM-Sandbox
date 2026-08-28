# Agent Rules — LLM Sandbox

## 0. Mission

Build and maintain the LLM Sandbox backend with security, correctness, scalability, performance, reproducibility and cost-efficiency as first-class constraints.

The project is a prompt-injection challenge. Protect the infrastructure and application boundaries without turning the intended LLM challenge into an unintentionally impossible prompt.

## 1. Scope Rules

- Backend first.
- Do not spend implementation time on frontend unless explicitly instructed.
- Prefer the smallest architecture that satisfies measured requirements.
- Do not introduce infrastructure because it sounds impressive.
- Every new dependency needs a concrete reason.

## 2. Security Guardrails

### Secrets
- NEVER commit secrets.
- NEVER print secrets.
- NEVER log API keys or Authorization headers.
- NEVER store participant-supplied provider credentials in PostgreSQL by default.
- NEVER expose secrets to the model context.
- Use environment injection or a secret manager.
- Rotate development credentials when exposed.

### System Prompt
- Treat the system prompt as challenge secret material.
- Never return it from public endpoints.
- Never include it in standard logs.
- Never put it in Redis jobs.
- Never put decrypted prompt text into error messages.
- Encrypt at rest.
- Decrypt only where execution requires it.

### User Input
Participant input is untrusted.

Never allow participant input to directly control:
- shell commands;
- SQL identifiers;
- provider URLs;
- filesystem paths;
- environment variable names;
- internal service addresses;
- authentication headers.

Use typed/validated parameters and allowlists.

### Network
- Only configured provider endpoints are allowed.
- No arbitrary outbound URL fetch.
- No participant-controlled callback URLs.
- Explicit HTTP timeouts are mandatory.
- TLS verification must remain enabled.

### Authorization
- Every run lookup must be authorized for the requesting user.
- Operator endpoints must be separate and protected.
- Never trust a user-supplied user ID for authorization.

## 3. LLM-Specific Rules

- Use a provider abstraction.
- Pin production provider/model configurations.
- Bound input size.
- Bound output tokens.
- Bound total execution time.
- Bound retries.
- Classify provider errors.
- Use circuit breakers for repeated provider failures.
- Do not retry deterministic 4xx failures.
- Do not blindly retry every 429/5xx forever.
- Do not allow participant input to select arbitrary models or providers.

The model output is untrusted data from the backend perspective.

Do not execute model output as:
- shell commands;
- SQL;
- code;
- URLs to internal services;
- configuration.

## 4. Prompt-Injection Challenge Rule

Do NOT over-harden the challenge's intended attack surface.

The correct design is:

```text
protect infrastructure
        +
protect credentials
        +
protect other users
        +
protect system internals
        +
leave the model-level challenge meaningful
```

Do not add hidden middleware whose only purpose is to automatically block every injection phrase unless explicitly requested by the challenge specification.

## 5. Database Rules

- Database schema must remain BCNF.
- Every non-trivial functional dependency must be checked before introducing a relation.
- Every determinant must be a candidate key/superkey.
- Use foreign keys for real domain relationships.
- Add constraints in the database, not only application code.
- Use migrations only.

### Indexing
- Index actual query patterns.
- Prefer composite indexes that match filtering + ordering.
- Do not index every column.
- Re-check indexes with `EXPLAIN (ANALYZE, BUFFERS)`.
- Remember every index adds write/storage cost.

### Pagination
- Use keyset pagination for large ordered histories.
- Do not use deep OFFSET pagination in hot paths.
- Cursor semantics must be deterministic.

### N+1
Never perform one DB query per:
- run;
- challenge;
- model;
- provider;
- response field;
- test item.

Prefer joins, eager loading or bounded batch queries.

### Transactions
- Keep transactions short.
- Never hold a DB transaction open during an LLM call.
- Use conditional state transitions for worker claims/finalization.
- Use pooled connections.

## 6. Backend Code Quality

Architecture:

```text
Route
  ↓
Service
  ↓
Repository / Provider adapter
  ↓
Infrastructure
```

Rules:
- routes handle HTTP concerns;
- services contain business rules;
- repositories contain DB access;
- provider adapters hide vendor-specific details;
- workers orchestrate execution.

Avoid:
- global mutable state;
- hidden background threads;
- circular dependencies;
- giant service classes;
- copy/pasted provider logic;
- shelling out when a library API exists.

## 7. Async and Concurrency

- Use async I/O for API and provider calls.
- Bound worker concurrency.
- Never use unbounded `asyncio.gather()` for jobs.
- Match concurrency to provider quotas and measured resource capacity.
- Use semaphores/queues where needed.
- Backpressure before saturation is better than failure after saturation.

## 8. Performance

Optimize the hot path first.

Measure:
- DB query count;
- DB latency;
- provider latency;
- queue wait;
- serialization cost;
- token usage;
- memory usage.

Do not micro-optimize code without measurements.

### Target principle

```text
fewer remote calls
fewer DB round trips
fewer tokens
bounded concurrency
shorter critical paths
```

## 9. Cost

Before adding a component, ask:
1. What problem does it solve?
2. Is the problem measured?
3. What is the cheapest solution?
4. Does it create new operational overhead?

Default:
- PostgreSQL over multiple databases;
- Redis over a distributed event platform;
- object storage over relational blobs;
- autoscaling workers over permanently oversized workers;
- one region unless resilience requirements justify more.

## 10. Observability

Logs must be:
- structured;
- redacted;
- correlation-ID based.

Metrics must include:
- accepted runs;
- queue depth;
- queue age;
- provider latency;
- error classes;
- retry count;
- token counts;
- cost;
- DB pool saturation.

Health endpoints must not call the LLM.

## 11. Testing Requirements

Every feature should have the smallest useful test set.

### Required categories
- unit;
- integration;
- security;
- performance;
- contract.

Add regression tests for:
- query count;
- authorization;
- secret redaction;
- retry limits;
- duplicate queue delivery;
- pagination ordering.

## 12. Error Handling

Never expose internal exceptions directly.

Map:
```text
ValidationError
ProviderUnavailable
ProviderRateLimited
ProviderTimeout
AuthorizationError
AdmissionRejected
PersistenceError
SystemError
```

Client responses must be safe and concise.

Internal logs may contain diagnostic detail only after redaction.

## 13. Git Rules — HARD GATE

### Without explicit user permission

The agent MUST NOT:
- commit;
- push;
- create a PR;
- merge;
- rewrite published history;
- force-push.

Editing files locally is allowed.

### Before permission

The agent may:
- create files;
- modify code;
- run tests;
- inspect git diff;
- run linters;
- run security checks.

The agent must stop before any commit/push/PR action.

### After explicit permission

The required workflow is:

```text
create new task branch
        ↓
implement
        ↓
run tests/lint/security checks
        ↓
review diff
        ↓
commit
        ↓
push branch
        ↓
create PR
        ↓
stop and wait for review
```

Never push directly to `main` or another protected branch unless separately and explicitly authorized.

Do not force-push unless separately authorized.

Do not amend or squash existing commits without permission.

## 14. Change Discipline

Before modifying architecture:
- inspect existing implementation;
- read relevant documentation;
- identify dependencies;
- preserve public contracts unless intentionally changing them;
- update docs when architecture changes.

Never silently replace a component just because another library is more fashionable.

## 15. Definition of Done for Code Changes

- [ ] Type checks pass.
- [ ] Formatter/linter passes.
- [ ] Unit tests pass.
- [ ] Integration tests relevant to the change pass.
- [ ] Security checks pass.
- [ ] Query count checked for hot-path DB changes.
- [ ] Performance impact considered.
- [ ] No secrets added.
- [ ] Documentation updated when behavior/architecture changes.
- [ ] Git permission gate respected.
