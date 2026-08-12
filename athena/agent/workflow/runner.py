"""One-tick deterministic orchestration for policy workflows."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from athena.agent.context.manager import ContextManager
from athena.agent.policy.agent import PolicyAgent, PolicyDecisionError
from athena.agent.policy.contracts import (
    ActionDecision,
    ToolCallV2,
    ToolResultV2,
    ToolSpecV2,
    ToolStatus,
)
from athena.agent.workflow.state import (
    OpsTaskPhase,
    OpsTaskState,
    OpsTaskStatus,
    TaskBudget,
)
from athena.api.task_store import (
    EvidenceStore,
    TaskEventRepository,
    TaskStateRepository,
)
from athena.tools.runtime import ToolRuntime, ToolRuntimeContext
from athena.types import JSONValue

if TYPE_CHECKING:
    from athena.api.auth import TenantContext


class WorkflowPolicy(Protocol):
    """Workflow-specific reduction and evidence completion boundary."""

    def fact_from_result(
        self, decision: ActionDecision, result: ToolResultV2
    ) -> dict[str, JSONValue] | None: ...

    def terminal_error(self, state: OpsTaskState) -> str | None: ...

    def is_complete(self, state: OpsTaskState) -> bool: ...


class WorkflowRunner:
    """Persist state around at most one decision and one tool invocation per tick."""

    def __init__(
        self,
        task_repository: TaskStateRepository,
        event_repository: TaskEventRepository,
        evidence_store: EvidenceStore,
        context_manager: ContextManager,
        policy_agent: PolicyAgent,
        tool_runtime: ToolRuntime,
        workflow: WorkflowPolicy | None = None,
    ) -> None:
        self._tasks = task_repository
        self._events = event_repository
        self._evidence = evidence_store
        self._context = context_manager
        self._policy = policy_agent
        self._tools = tool_runtime
        self._workflow = workflow

    async def tick(
        self,
        tenant: TenantContext,
        task_id: str,
        tool_context: ToolRuntimeContext,
        tool_specs: tuple[ToolSpecV2, ...],
    ) -> OpsTaskState:
        tick_started = time.monotonic()
        state = self._tasks.load(tenant, task_id)
        if state is None:
            raise KeyError("ops task not found")
        if state.status in {
            OpsTaskStatus.SUCCEEDED,
            OpsTaskStatus.FAILED,
            OpsTaskStatus.CANCELLED,
        }:
            return state
        if (
            tool_context.tenant_id != state.tenant_id
            or tool_context.environment_id != state.environment_id
        ):
            raise PermissionError("tool context does not match task scope")
        namespace = state.scope.get("namespace")
        if tool_context.allowed_namespaces and (
            not isinstance(namespace, str)
            or namespace not in tool_context.allowed_namespaces
        ):
            raise PermissionError("tool context expands the persisted task scope")
        if not tool_context.readonly_only or any(
            not spec.readonly for spec in tool_specs
        ):
            raise PermissionError("policy workflows accept readonly tools only")

        running = (
            state.transition_to(OpsTaskStatus.RUNNING, OpsTaskPhase.COLLECT)
            if state.status in {OpsTaskStatus.QUEUED, OpsTaskStatus.WAITING}
            else state
        )
        if running is not state:
            self._tasks.save(
                tenant, running, expected_state_version=state.state_version
            )
            self._events.append(
                tenant, task_id, "task.started", {"phase": running.phase.value}
            )
        budget_error = self._budget_error(running.budget)
        if budget_error is not None:
            return self._fail(tenant, running, budget_error)
        evidence = self._evidence.list_for_task(tenant, task_id)
        if any(
            item.data_origin.value != running.environment_mode.value
            for item in evidence
        ):
            return self._fail(tenant, running, "EVIDENCE_ORIGIN_MISMATCH")
        context = self._context.build(
            running,
            evidence,
            tool_specs,
            allowed_capabilities=tool_context.allowed_capabilities,
            content_loader=lambda item: self._evidence.get_content(
                tenant, item.id
            ),
        )
        self._events.append(
            tenant,
            task_id,
            "context.compressed",
            {
                key: value
                for key, value in context.compression_metrics.items()
                if isinstance(value, int)
            },
        )
        if context.estimated_tokens > running.budget.remaining_tokens:
            return self._fail(tenant, running, "TASK_TOKEN_BUDGET_EXHAUSTED")
        try:
            decision = await self._policy.decide(context)
        except PolicyDecisionError as exc:
            return self._fail(
                tenant,
                running,
                str(getattr(exc, "error_code", "POLICY_DECISION_INVALID")),
                expected=running.state_version,
            )

        latest = self._tasks.load(tenant, task_id)
        if latest is not None and latest.state_version != running.state_version:
            if latest.status is OpsTaskStatus.CANCELLED:
                return latest
            raise RuntimeError("task state changed during policy decision")

        if self._made_no_progress(running, decision):
            return self._fail(
                tenant,
                running,
                "WORKFLOW_NO_PROGRESS",
                expected=running.state_version,
            )

        self._events.append(
            tenant,
            task_id,
            "decision.recorded",
            {
                "action": decision.action,
                "reason_code": decision.reason_code,
                "confidence": decision.confidence,
            },
        )
        self._events.append(
            tenant,
            task_id,
            "tool.started",
            {"action": decision.action},
        )
        result = await self._tools.invoke(
            ToolCallV2(
                call_id=f"tool-{uuid.uuid4().hex}",
                task_id=task_id,
                tenant_id=state.tenant_id,
                tool_name=decision.action,
                arguments=decision.arguments,
            ),
            tool_context,
        )
        for evidence_id in result.evidence_refs:
            self._events.append(
                tenant,
                task_id,
                "evidence.created",
                {"evidence_id": evidence_id, "action": decision.action},
            )
        self._events.append(
            tenant,
            task_id,
            "tool.finished",
            {
                "action": decision.action,
                "status": result.status.value,
                "evidence_refs": list(result.evidence_refs),
                "error_code": result.error_code,
                "retryable": result.retryable,
            },
        )

        latest = self._tasks.load(tenant, task_id)
        if latest is not None and latest.state_version != running.state_version:
            if latest.status is OpsTaskStatus.CANCELLED:
                return latest
            if latest.status in {
                OpsTaskStatus.SUCCEEDED,
                OpsTaskStatus.FAILED,
            }:
                return latest
            raise RuntimeError("task state changed during readonly tool execution")

        elapsed_ms = max(1, int((time.monotonic() - tick_started) * 1000))
        next_budget = TaskBudget(
            remaining_steps=running.budget.remaining_steps - 1,
            remaining_tokens=max(
                0, running.budget.remaining_tokens - context.estimated_tokens
            ),
            remaining_time_ms=max(
                0, running.budget.remaining_time_ms - elapsed_ms
            ),
        )
        if result.status is ToolStatus.SUCCEEDED:
            fact: Mapping[str, JSONValue] = {
                "action": decision.action,
                "evidence_ids": list(result.evidence_refs),
            }
            if self._workflow is not None:
                workflow_fact = self._workflow.fact_from_result(decision, result)
                if workflow_fact is not None:
                    fact = workflow_fact
            next_state = replace(
                running,
                budget=next_budget,
                completed_actions=(*running.completed_actions, decision),
                action_history=(*running.action_history, decision),
                facts=(*running.facts, dict(fact)),
                state_version=running.state_version + 1,
            )
        else:
            next_state = replace(
                running,
                budget=next_budget,
                failed_actions=(*running.failed_actions, decision),
                action_history=(*running.action_history, decision),
                state_version=running.state_version + 1,
            )
        terminal_error: str | None = None
        complete = self._workflow_complete(next_state)
        exhausted = self._budget_error(next_budget)
        if result.status is not ToolStatus.SUCCEEDED:
            terminal_error = result.error_code or "TOOL_EXECUTION_FAILED"
        elif complete:
            if self._workflow is not None:
                terminal_error = self._workflow.terminal_error(next_state)
        elif exhausted is not None:
            terminal_error = exhausted

        if result.status is not ToolStatus.SUCCEEDED or exhausted is not None or complete:
            terminal_status = OpsTaskStatus.FAILED
            if result.status is ToolStatus.SUCCEEDED and terminal_error is None:
                terminal_status = OpsTaskStatus.SUCCEEDED
            next_state = replace(
                next_state,
                status=terminal_status,
                phase=OpsTaskPhase.REPORT,
            )
        self._tasks.save(
            tenant, next_state, expected_state_version=running.state_version
        )
        if next_state.status in {OpsTaskStatus.SUCCEEDED, OpsTaskStatus.FAILED}:
            self._events.append(
                tenant,
                task_id,
                (
                    "task.completed"
                    if next_state.status is OpsTaskStatus.SUCCEEDED
                    else "task.failed"
                ),
                {
                    "phase": next_state.phase.value,
                    "error_code": (
                        None
                        if next_state.status is OpsTaskStatus.SUCCEEDED
                        else (
                            terminal_error
                            or result.error_code
                            or "TOOL_EXECUTION_FAILED"
                        )
                    ),
                },
            )
        return next_state

    def fail_task(
        self, tenant: TenantContext, task_id: str, error_code: str
    ) -> OpsTaskState:
        """Force a non-terminal task to a persisted, observable failure state."""
        state = self._tasks.load(tenant, task_id)
        if state is None:
            raise KeyError("ops task not found")
        if state.status in {
            OpsTaskStatus.SUCCEEDED,
            OpsTaskStatus.FAILED,
            OpsTaskStatus.CANCELLED,
        }:
            return state
        if state.status in {OpsTaskStatus.QUEUED, OpsTaskStatus.WAITING}:
            running = state.transition_to(OpsTaskStatus.RUNNING, OpsTaskPhase.COLLECT)
            self._tasks.save(
                tenant, running, expected_state_version=state.state_version
            )
            self._events.append(
                tenant, task_id, "task.started", {"phase": running.phase.value}
            )
            state = running
        return self._fail(tenant, state, error_code)

    @staticmethod
    def _made_no_progress(state: OpsTaskState, decision: object) -> bool:
        """Reject a third consecutive identical action, successful or failed."""
        if not isinstance(decision, ActionDecision):
            return False
        history = state.action_history or (
            *state.completed_actions,
            *state.failed_actions,
        )
        if len(history) < 2:
            return False

        def signature(item: ActionDecision) -> tuple[str, str]:
            return (
                item.action,
                json.dumps(item.arguments, sort_keys=True, separators=(",", ":")),
            )

        current = signature(decision)
        return signature(history[-1]) == current == signature(history[-2])

    def _workflow_complete(self, state: OpsTaskState) -> bool:
        if self._workflow is None:
            return state.budget.remaining_steps == 0
        checker = getattr(self._workflow, "is_complete", None)
        return bool(checker(state)) if callable(checker) else False

    @staticmethod
    def _budget_error(budget: TaskBudget) -> str | None:
        if budget.remaining_steps <= 0:
            return "TASK_STEP_BUDGET_EXHAUSTED"
        if budget.remaining_tokens <= 0:
            return "TASK_TOKEN_BUDGET_EXHAUSTED"
        if budget.remaining_time_ms <= 0:
            return "TASK_TIME_BUDGET_EXHAUSTED"
        return None

    def _fail(
        self,
        tenant: TenantContext,
        state: OpsTaskState,
        error_code: str,
        *,
        expected: int | None = None,
    ) -> OpsTaskState:
        failed = state.transition_to(OpsTaskStatus.FAILED, OpsTaskPhase.REPORT)
        self._tasks.save(
            tenant,
            failed,
            expected_state_version=(
                state.state_version if expected is None else expected
            ),
        )
        self._events.append(
            tenant, state.task_id, "task.failed", {"error_code": error_code}
        )
        return failed
