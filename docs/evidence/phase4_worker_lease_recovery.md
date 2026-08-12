# Phase 4 Worker Lease, Checkpoint and Recovery Evidence

Date: 2026-07-19

Scope:
- P4-03 Worker lease, checkpoint and recovery

Acceptance evidence:
- A queued task can be claimed by one worker and completed only after a durable checkpoint.
- A stale worker cannot checkpoint after its `state_version` or `lease_generation` is outdated.
- An expired lease can be reclaimed by another worker; the old worker is fenced out.
- Concurrent claim attempts for one queued task grant at most one lease.
- Worker retry writes a durable queued checkpoint and republishes through the outbox.
- Exhausted retry writes failed task state before dead-lettering the stream message.
- Stream ACK happens only after checkpoint/requeue/dead-letter state has been durably written.

Focused tests:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_durable_repositories.py tests\test_durable_worker.py -q
8 passed
```

Durable regression:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_durable_repositories.py tests\test_durable_worker.py tests\test_durable_ops_task_api.py tests\test_durable_evidence.py tests\test_durable_alert_api.py tests\test_operation_plan_approval_api.py tests\test_tool_effect_repository.py -q
19 passed
```

Rollback note:
- Disable the durable worker process and continue serving API-only readonly paths.
- Existing queued tasks remain in PostgreSQL; they can be resumed after rolling forward or processed by the previous worker image if schema compatibility is retained.
- The repository CAS claim change is backward compatible with existing rows because it only tightens claim conditions around `state_version`, `status`, `next_run_at`, and lease fields.
