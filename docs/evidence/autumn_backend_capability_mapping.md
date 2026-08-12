# Autumn Backend Capability Track Mapping Evidence

Date: 2026-07-19

## Scope

This evidence maps completed Phase work to the autumn recruiting backend
capability track.

## Completed mappings

### B1 Durable Alert Ingest

Implemented by durable Alertmanager ingress and task repository transactions:

- alert receipt
- canonical fingerprint
- duplicate replay
- active alert instance
- task creation/reuse
- task event
- transactional outbox
- tenant scope

### B4 Queue and Recovery / API-Worker separation

Implemented by:

- `athena worker` CLI command
- public bootstrap split
- transactional outbox relay
- durable worker lease
- lease generation / state-version fencing
- per-tick checkpoint
- retry and terminal failure handling
- crash recovery through expired leases

### B5 Agent Governance and external-effect idempotency

Implemented by:

- PolicyAgent + ToolRuntime readonly governance
- risk/capability/schema/scope checks
- immutable OperationPlan and canonical hash
- approval lifecycle
- disabled-by-default S3 write execution
- idempotent `ToolEffect` records with `plan_hash`

### B0 Domain baseline

Implemented by:

- frozen OpsTask status/phase state machine
- stable domain error-code families
- Tool V2 / Evidence / Task contract tests
- reproducible workload matrix in `benchmarks/backend-baseline/workloads.json`

### B6 Evidence layering and shard evolution

Implemented by:

- metadata in SQLAlchemy repository and content-addressed raw content store
- tenant/task hash-prefix content layout
- hash verification for tamper detection
- retention policy with legal hold
- shard router that refuses physical sharding without a benchmark report ID

### B7 Capacity, micro-batch and rate limiting

Implemented by:

- bounded micro-batch helper with max batch size, max wait and max concurrency
- partial batch failure isolation
- hierarchical token bucket with weighted cost
- global, tenant, route and model bucket dimensions

### B8 TraceContext and redaction

Implemented by:

- W3C `traceparent` middleware normalization
- task/outbox/stream/worker trace propagation
- LLM/tool decision trace redaction
- replay linkage helpers that connect tenant/task/run/call identifiers without raw prompt, secret or thought material

## Acceptance evidence

Focused combined regression:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_durable_alert_api.py tests\test_durable_repositories.py tests\test_durable_worker.py tests\test_durable_ops_task_api.py tests\test_operation_plan_approval_api.py tests\test_tool_runtime.py tests\test_policy_workflow.py tests\test_tool_effect_repository.py -q
36 passed in 6.79s
```

B0/B6/B7/B8 focused regression:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_durable_evidence.py tests\test_batching_capacity.py tests\test_trace_context_linkage.py tests\test_resilience.py -q
19 passed in 2.35s
```

## Not yet mapped

- B2 still needs real PostgreSQL acceptance, not only SQLite/Alembic rendering.
- B3 still needs real Redis Streams integration evidence.
- B9 still requires failure-injection, component-level load and before/after benchmark artifacts.
- B10 requires one-command live demo evidence and walkthrough artifacts.
