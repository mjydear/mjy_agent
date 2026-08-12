# Phase 4 Durable Alertmanager Ingress Evidence

Date: 2026-07-19

## Scope

This evidence covers P4-05 Durable Alertmanager receipt, deduplication and retry.

## Implementation facts

- `/api/alerts/webhook` uses `DurableAlertService` when a database-backed
  `TaskRepository` is configured.
- Each accepted alert writes receipt, active alert instance, task, initial task
  event and outbox message as one durable repository command.
- Duplicate payloads replay the original receipt and task instead of creating a
  second task.
- Batch payloads create/reuse one task per canonical fingerprint and redact
  sensitive metadata before persistence.
- Alertmanager machine credentials are supported with `X-Alert-Token`, mapped by
  `settings.security.alert_integration_tokens` or
  `ATHENA_ALERT_INTEGRATION_TOKENS=token:tenant`.
- Production or auth-enabled webhook ingress fails closed unless the caller is an
  alert integration principal with `alerts:ingest` or another authenticated
  principal explicitly has that scope.
- Production readiness reports `ALERT_INTEGRATION_TOKEN_REQUIRED` when no machine
  integration credential is configured.
- Retry/recovery is handled through the transactional outbox and worker retry
  path: delivery is at-least-once, and task execution remains idempotent through
  durable state/version/checkpoint records.

## Acceptance evidence

Focused tests:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_durable_alert_api.py tests\test_alerts_webhook.py tests\test_tenant_resource_isolation.py tests\test_health.py tests\test_llm_config_secrets.py -q
30 passed in 14.96s
```

Previously recorded worker/outbox recovery regression:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_durable_repositories.py tests\test_durable_worker.py tests\test_durable_ops_task_api.py tests\test_durable_alert_api.py -q
11 passed in 3.60s
```

## Environment limitation

The local Docker daemon is not running:

```text
failed to connect to the docker API at npipe:////./pipe/docker_engine
```

So this evidence does not claim a real PostgreSQL + Redis Streams integration
run. That remains covered by the unchecked real-infrastructure tasks.

## Rollback note

Remove `database.url` or disable database-backed app wiring to return the webhook
to the legacy synchronous demo path. Remove `alert_integration_tokens` to disable
machine-token ingress in production; production readiness will then fail closed.
