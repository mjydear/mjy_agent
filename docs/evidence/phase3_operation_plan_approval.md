# Phase 3 OperationPlan and Approval Evidence

Date: 2026-07-19

Scope:
- P3-01 Immutable OperationPlan and canonical plan hash
- P3-02 Approval lifecycle, scopes and APIs
- P3-03 Controlled S3 execution path, disabled by default

Acceptance evidence:
- OperationPlan canonical hash is stable under JSON key reordering.
- Duplicate tenant + plan hash replays the existing immutable plan.
- Plans and approvals are tenant scoped.
- Approval requires pending state, exact plan hash, valid target scope and non-expired plan.
- Execution is blocked while `ops.security.default_readonly=true`.
- Execution requires approved plan, approved approval, matching plan hash and `cloud:execute`.
- `Idempotency-Key` is persisted as ToolEffect call id; repeated execution replays the stored result.
- ToolEffect stores `plan_hash`, request hash, result and post-condition verification.

Focused tests:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_operation_plan_approval_api.py tests\test_tool_effect_repository.py -q
7 passed
```

Compatibility regression:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_operation_plan_approval_api.py tests\test_tool_effect_repository.py tests\test_environment_api.py tests\test_cloud_ops.py tests\test_k8s_readonly_client.py tests\test_llm_config_secrets.py tests\test_tenant_observability_api.py tests\test_tenant_resource_isolation.py tests\test_tenant_workflow_compatibility_api.py -q
79 passed
```

Migration check:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m alembic upgrade head --sql
Generated migration chain through 20260719_0004.
```

Rollback note:
- Disable write execution immediately by setting `ops.security.default_readonly=true`.
- Roll back API exposure by removing the `approvals` routers from `athena/api/server.py`.
- Database rollback is `alembic downgrade 20260719_0003`; this drops `approvals` and `operation_plans` only.
- Existing readonly OpsTask and legacy CloudOps APIs remain compatible because legacy CloudOps execution is unchanged unless the durable operation-plan path is used.
