# Autumn backend B0/B6/B7/B8 evidence

Date: 2026-07-19

## Scope

This evidence covers local, reproducible backend capability gates:

- B0 domain contracts, state machine, error-code boundaries and baseline workload matrix.
- B6 Evidence metadata/content separation, hash verification, retention policy and benchmark-gated shard routing.
- B7 bounded micro-batch, downstream isolation and weighted hierarchical rate limiting.
- B8 W3C TraceContext propagation, redaction and replay linkage across queue boundaries.

It does not claim LIVE Kubernetes, browser behavior or release load-test success.

## Code paths

- `benchmarks/backend-baseline/workloads.json`
- `athena/agent/workflow/state.py`
- `athena/exceptions.py`
- `athena/infra/evidence_content.py`
- `athena/api/repositories/evidence_repository.py`
- `athena/infra/batching.py`
- `athena/infra/resilience.py`
- `athena/observability/trace_context.py`
- `athena/infra/task_stream.py`
- `athena/application/outbox_relay.py`
- `athena/application/durable_worker.py`

## Verification

```powershell
$env:PYTHONPYCACHEPREFIX='D:\tmp\mjy_agent\pycache'
$env:PYTEST_ADDOPTS='-o cache_dir=D:\tmp\mjy_agent\pytest_cache'
D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_durable_evidence.py tests\test_batching_capacity.py tests\test_trace_context_linkage.py tests\test_resilience.py -q
```

Result:

```text
19 passed in 2.35s
```

## Notes

- Physical Evidence sharding remains disabled until a benchmark report ID is supplied to the shard router.
- Redis-backed token bucket support is implemented through Lua; the focused test uses the in-memory backend to prove the same semantics without requiring a live Redis daemon.
- Trace payload redaction treats prompt, thought and secret-bearing keys as forbidden durable material.
