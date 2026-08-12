"""Application service for tenant-scoped Phase 1 OpsTask facts."""

from __future__ import annotations

import logging
import hashlib
import re
import uuid
from dataclasses import asdict
from typing import TYPE_CHECKING

from athena.agent.policy.contracts import (
    EnvironmentMode,
    ExecutionProfile,
    RiskLevel,
    ToolSpecV2,
)
from athena.agent.workflow.state import OpsTaskState, OpsTaskStatus, TaskBudget
from athena.api.auth import TenantContext
from athena.api.task_store import (
    EvidenceStore,
    TaskEventRepository,
    TaskStateRepository,
)
from athena.memory.evidence import Evidence
from athena.tools.runtime import ToolRuntimeContext

if TYPE_CHECKING:
    from athena.agent.workflow.runner import WorkflowRunner
    from athena.api.task_manager import AsyncTaskManager

logger = logging.getLogger(__name__)


class OpsTaskService:
    """Own task lifecycle facts; it neither performs tools nor chooses permissions."""

    def __init__(
        self,
        tasks: TaskStateRepository,
        events: TaskEventRepository,
        evidence: EvidenceStore,
        *,
        environment_mode: EnvironmentMode,
        allowed_namespaces: frozenset[str],
        workflow_runner: WorkflowRunner | None = None,
        tool_specs: tuple[ToolSpecV2, ...] = (),
    ) -> None:
        self._tasks = tasks
        self._events = events
        self._evidence = evidence
        self._environment_mode = environment_mode
        self._allowed_namespaces = allowed_namespaces
        self._runner = workflow_runner
        self._tool_specs = tool_specs

    def create(
        self, tenant: TenantContext, objective: str, environment_id: str, namespace: str
    ) -> OpsTaskState:
        if not objective.strip() or not environment_id.strip() or not namespace.strip():
            raise ValueError("objective, environment_id, and namespace are required")
        if self._allowed_namespaces and namespace not in self._allowed_namespaces:
            raise PermissionError("OPS_NAMESPACE_FORBIDDEN")
        state = OpsTaskState(
            task_id=f"ops-{uuid.uuid4().hex}",
            tenant_id=tenant.tenant_id,
            objective=objective.strip(),
            environment_id=environment_id.strip(),
            environment_mode=self._environment_mode,
            scope={"namespace": namespace.strip()},
            tenant_policy_snapshot={"readonly": True},
            budget=TaskBudget(
                remaining_steps=4, remaining_tokens=6000, remaining_time_ms=30000
            ),
            execution_profile=ExecutionProfile.BOUNDED_POLICY_LOOP,
        )
        self._tasks.save(tenant, state)
        self._events.append(
            tenant, state.task_id, "task.created", {"phase": "validate"}
        )
        return state

    def schedule(
        self, manager: AsyncTaskManager, tenant: TenantContext, task_id: str
    ) -> str:
        """Submit the persisted task to the process-local Phase 1 executor."""
        if self._runner is None or not self._tool_specs:
            raise RuntimeError("policy workflow execution is not configured")
        return manager.submit(
            lambda: self.run_to_terminal(tenant, task_id),
            tenant_id=tenant.tenant_id,
            kind="ops-workflow",
        )

    async def run_to_terminal(
        self, tenant: TenantContext, task_id: str
    ) -> dict[str, object]:
        """Tick the bounded workflow until it reaches a persisted terminal state."""
        if self._runner is None:
            raise RuntimeError("policy workflow execution is not configured")
        state = self.get(tenant, task_id)
        if state is None:
            raise KeyError("ops task not found")
        max_ticks = state.budget.remaining_steps + 3
        try:
            for _ in range(max_ticks):
                if state.status in {
                    OpsTaskStatus.SUCCEEDED,
                    OpsTaskStatus.FAILED,
                    OpsTaskStatus.CANCELLED,
                }:
                    return self.state_view(state)
                state = await self._runner.tick(
                    tenant,
                    task_id,
                    self._tool_context(state),
                    self._tool_specs,
                )
            state = self._runner.fail_task(
                tenant, task_id, "WORKFLOW_TICK_LIMIT_EXCEEDED"
            )
        except Exception:  # noqa: BLE001 - background failures become task facts
            logger.exception("OpsTask workflow failed task_id=%s", task_id)
            state = self._runner.fail_task(tenant, task_id, "WORKFLOW_EXECUTION_FAILED")
        return self.state_view(state)

    def _tool_context(self, state: OpsTaskState) -> ToolRuntimeContext:
        namespace = state.scope.get("namespace")
        if not isinstance(namespace, str) or not namespace.strip():
            raise PermissionError("ENV_SCOPE_INVALID")
        capabilities = frozenset(
            capability
            for spec in self._tool_specs
            for capability in spec.required_capabilities
        )
        return ToolRuntimeContext(
            tenant_id=state.tenant_id,
            environment_id=state.environment_id,
            allowed_capabilities=capabilities,
            allowed_tool_names=frozenset(spec.name for spec in self._tool_specs),
            allowed_namespaces=frozenset({namespace}),
            max_risk_level=RiskLevel.S1,
            readonly_only=True,
        )

    def get(self, tenant: TenantContext, task_id: str) -> OpsTaskState | None:
        return self._tasks.load(tenant, task_id)

    def list(self, tenant: TenantContext) -> tuple[OpsTaskState, ...]:
        return self._tasks.list(tenant)

    def cancel(self, tenant: TenantContext, task_id: str) -> OpsTaskState:
        current = self._tasks.load(tenant, task_id)
        if current is None:
            raise KeyError("task state not found")
        state = self._tasks.request_cancel(tenant, task_id)
        if (
            current.status is not OpsTaskStatus.CANCELLED
            and state.status is OpsTaskStatus.CANCELLED
        ):
            self._events.append(
                tenant, task_id, "task.cancelled", {"phase": state.phase.value}
            )
        return state

    def add_input(
        self, tenant: TenantContext, task_id: str, content: str
    ) -> OpsTaskState:
        state = self.get(tenant, task_id)
        if state is None:
            raise KeyError("ops task not found")
        if not content.strip():
            raise ValueError("input must be non-empty")
        if state.status is not OpsTaskStatus.WAITING:
            raise ValueError("TASK_NOT_WAITING_INPUT")
        redacted = self._redact_operator_input(content.strip())
        self._events.append(
            tenant,
            task_id,
            "task.input_received",
            {
                "summary": redacted[:256],
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            },
        )
        running = state.transition_to(OpsTaskStatus.RUNNING, state.phase)
        self._tasks.save(
            tenant, running, expected_state_version=state.state_version
        )
        return running

    @staticmethod
    def _redact_operator_input(content: str) -> str:
        pattern = re.compile(
            r"(?i)\b(authorization|password|secret|token|api[_-]?key)"
            r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
        )
        return pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", content)

    def events_after(
        self, tenant: TenantContext, task_id: str, after_sequence: int = 0
    ):
        if self.get(tenant, task_id) is None:
            raise KeyError("ops task not found")
        return self._events.list_after(tenant, task_id, after_sequence)

    def evidence_for_task(
        self, tenant: TenantContext, task_id: str
    ) -> tuple[Evidence, ...]:
        if self.get(tenant, task_id) is None:
            raise KeyError("ops task not found")
        return self._evidence.list_for_task(tenant, task_id)

    def report(self, tenant: TenantContext, task_id: str) -> dict[str, object]:
        state = self.get(tenant, task_id)
        if state is None:
            raise KeyError("ops task not found")
        root_causes = [
            {
                "summary": fact["root_cause"],
                "evidence_ids": fact.get("root_cause_evidence_ids", []),
            }
            for fact in state.facts
            if isinstance(fact.get("root_cause"), str)
        ]
        return {
            "task": self.detail_view(tenant, state),
            "evidence_count": len(self.evidence_for_task(tenant, task_id)),
            "root_causes": root_causes,
        }

    def detail_view(
        self, tenant: TenantContext, state: OpsTaskState
    ) -> dict[str, object]:
        view = self.state_view(state)
        events = self._events.list_after(tenant, state.task_id, 0)
        evidence = self._evidence.list_for_task(tenant, state.task_id)
        last_error_code = next(
            (
                item.data.get("error_code")
                for item in reversed(events)
                if item.event_type == "task.failed"
                and isinstance(item.data.get("error_code"), str)
            ),
            None,
        )
        view.update(
            {
                "event_count": len(events),
                "evidence_count": len(evidence),
                "degraded": last_error_code is not None,
                "degradation_reason_code": last_error_code,
            }
        )
        return view

    @staticmethod
    def state_view(state: OpsTaskState) -> dict[str, object]:
        return {
            "id": state.task_id,
            "status": state.status.value,
            "phase": state.phase.value,
            "objective": state.objective,
            "environment_id": state.environment_id,
            "environment_mode": state.environment_mode.value,
            "scope": state.scope,
            "budget": asdict(state.budget),
            "state_version": state.state_version,
        }
