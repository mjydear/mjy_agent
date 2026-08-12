# Phase 2 Environment and Secret Persistence Evidence

Date: 2026-07-19

## Scope

This evidence covers:

- P2-01 Environment domain, repository and connection APIs.
- P2-02 SecretStore and managed LLM credential migration path.
- P2-03 Tenant-aware health and fallback presentation.

## Implementation facts

- Environment declarations are tenant-scoped SQLAlchemy facts in `environments`,
  with `EnvironmentRepository` and `/api/environments` CRUD/test/sync APIs.
- Environment connection tests return status, backend, capabilities and scoped
  error codes without returning credentials.
- Managed LLM metadata is stored in `llm_configs`; encrypted secret material is
  stored separately in `secret_records`.
- API/browser responses expose only `has_api_key` and `****suffix`.
- Production mode rejects legacy request-body LLM credentials, so new writes go
  through SecretStore references instead of plaintext cache/API payloads.
- `create_app()` automatically wires the durable LLM config store when
  `database.url` is configured; demo mode without a database keeps the existing
  local encrypted store.
- `/readyz` presents configured backend, active backend, status and stable
  reason codes for configuration, cache, database, Kubernetes and Prometheus.
- Tenant-scoped read APIs keep trace, benchmark and audit facts isolated; cross
  tenant reads return the same not-found style as missing resources unless an
  explicit auditor scope is present.
- Frontend projections reduce degraded/forbidden/error responses into safe
  browser-facing states without surfacing backend exception text or credentials.

## Acceptance evidence

Focused tests:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_llm_config_secrets.py -q
13 passed in 6.18s

D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_llm_config_secrets.py tests\test_environment_api.py tests\test_tenant_resource_isolation.py tests\test_operation_plan_approval_api.py tests\test_tool_effect_repository.py -q
22 passed in 7.72s
```

Durable/approval regression:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_durable_repositories.py tests\test_durable_worker.py tests\test_durable_ops_task_api.py tests\test_durable_alert_api.py -q
11 passed in 3.60s
```

Tenant-aware health/fallback presentation:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_health.py tests\test_tenant_observability_api.py tests\test_tenant_workflow_compatibility_api.py -q
9 passed in 9.60s

node --experimental-default-type=module --test tests\test_frontend_projections.mjs
7 tests passed
```

Migration rendering:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m alembic upgrade head --sql
Generated through revision 20260719_0005_llm_configs_secrets
```

Code health:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m compileall -q athena tests
git diff --check
```

`git diff --check` reported only existing LF/CRLF warnings and no whitespace
errors.

## Persistence and redaction checks

`test_durable_llm_config_and_secret_survive_app_restart_without_plaintext` creates
a managed LLM config, restarts the FastAPI app against the same SQLite database
file on `D:`, verifies the tenant can list and resolve the credential, verifies a
second tenant cannot see or resolve it, and inspects `llm_configs` plus
`secret_records` directly to prove the plaintext API key is absent.

`test_durable_secret_store_deletes_rotated_credential` rotates a managed
credential, verifies the old `credential_ref` is unreadable, and inspects the DB
to prove neither old nor new plaintext appears in `secret_records`.

## Rollback note

The new durable path is gated by `database.url`. If a deployment must roll back,
unset the database URL to return to the local encrypted demo store, or downgrade
Alembic revision `20260719_0005` to drop only `llm_configs` and
`secret_records`. The rollback does not affect task, approval, environment,
outbox or evidence tables.
