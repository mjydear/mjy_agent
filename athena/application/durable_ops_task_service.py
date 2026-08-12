"""Application service for PostgreSQL-backed OpsTask API commands."""

from __future__ import annotations

import hashlib
import json
import uuid

from athena.api.auth import TenantContext
from athena.api.repositories import PersistedTask, TaskCreate, TaskRepository


class DurableOpsTaskService:
    def __init__(
        self,
        tasks: TaskRepository,
        *,
        environment_mode: str,
        allowed_namespaces: frozenset[str],
        config_snapshot: dict[str, object],
    ) -> None:
        self._tasks = tasks
        self._environment_mode = environment_mode
        self._allowed_namespaces = allowed_namespaces
        self._config_snapshot = config_snapshot

    async def create(
        self,
        tenant: TenantContext,
        objective: str,
        environment_id: str,
        namespace: str,
        *,
        idempotency_key: str,
        request_hash: str,
        traceparent: str | None,
    ) -> tuple[PersistedTask, bool]:
        if self._allowed_namespaces and namespace not in self._allowed_namespaces:
            raise PermissionError("OPS_NAMESPACE_FORBIDDEN")
        command = TaskCreate(
            task_id=f"ops-{uuid.uuid4().hex}",
            tenant_id=tenant.tenant_id,
            objective=objective.strip(),
            environment_id=environment_id.strip(),
            environment_mode=self._environment_mode,
            scope={"namespace": namespace.strip()},
            policy_snapshot={"readonly": True, "version": "policy-v1"},
            config_snapshot=dict(self._config_snapshot),
            budget={
                "remaining_steps": 4,
                "remaining_tokens": 6000,
                "remaining_time_ms": 30000,
            },
            execution_profile="bounded_policy_loop",
            traceparent=traceparent,
        )
        return await self._tasks.create_task(
            command,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

    async def get(self, tenant: TenantContext, task_id: str) -> PersistedTask | None:
        return await self._tasks.get_task(tenant.tenant_id, task_id)

    async def list(
        self,
        tenant: TenantContext,
        *,
        status: str | None = None,
        environment_id: str | None = None,
        limit: int = 100,
    ) -> tuple[PersistedTask, ...]:
        return await self._tasks.list_tasks(
            tenant.tenant_id,
            status=status,
            environment_id=environment_id,
            limit=limit,
        )

    async def cancel(
        self,
        tenant: TenantContext,
        task_id: str,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[PersistedTask | None, bool]:
        return await self._tasks.cancel_task(
            tenant.tenant_id,
            task_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

    async def add_input(
        self,
        tenant: TenantContext,
        task_id: str,
        content: str,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[PersistedTask | None, bool]:
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return await self._tasks.resume_with_input(
            tenant.tenant_id,
            task_id,
            content_hash,
            self._redact(content),
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

    async def events_after(
        self, tenant: TenantContext, task_id: str, after_sequence: int = 0
    ) -> tuple[dict[str, object], ...]:
        return await self._tasks.list_events_after(
            tenant.tenant_id, task_id, after_sequence
        )

    @staticmethod
    def state_view(task: PersistedTask) -> dict[str, object]:
        return {
            "id": task.task_id,
            "status": task.status,
            "phase": task.phase,
            "objective": task.objective,
            "environment_id": task.environment_id,
            "environment_mode": task.environment_mode,
            "workflow_type": task.workflow_type,
            "scope": task.scope,
            "budget": task.budget,
            "state_version": task.state_version,
            "checkpoint_version": task.checkpoint_version,
            "lease_generation": task.lease_generation,
        }

    async def detail_view(
        self, tenant: TenantContext, task: PersistedTask
    ) -> dict[str, object]:
        view = self.state_view(task)
        events = await self.events_after(tenant, task.task_id)
        view.update(
            {
                "event_count": len(events),
                "evidence_count": len(task.state.get("evidence_ids", [])),
                "degraded": task.status == "failed",
                "degradation_reason_code": task.state.get("error_code"),
            }
        )
        return view

    @staticmethod
    def _redact(content: str) -> str:
        # The durable event keeps a reviewable summary, not raw operator input.
        return content.replace("\n", " ").strip()[:256]

    @staticmethod
    def request_hash(payload: object) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
