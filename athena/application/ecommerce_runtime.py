"""Application facade for running backend diagnosis through AgentRuntime."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from athena.backend import BackendReadOnlyToolCatalog, BackendToolAdapter
from athena.runtime import (
    AgentRuntime,
    AgentTask,
    Decision,
    DecisionEngine,
    DecisionKind,
    InMemoryRuntimeStore,
    TaskProfile,
    TaskStatus,
)
from athena.runtime.models import RuntimeSnapshot


class SequenceDecisionEngine:
    """Deterministic decision engine used by offline adapter acceptance tests."""

    def __init__(self, decisions: tuple[Decision, ...]) -> None:
        if not decisions:
            raise ValueError("at least one decision is required")
        self._decisions = decisions

    def decide(self, context: Any) -> Decision:
        index = context.tick_sequence - 1
        if index >= len(self._decisions):
            return Decision(
                kind=DecisionKind.FAIL,
                reason_code="DECISION_PLAN_EXHAUSTED",
                response="The decision plan was exhausted before a final result.",
            )
        return self._decisions[index]


@dataclass(frozen=True)
class EcommerceRuntimeMetrics:
    tick_count: int
    tool_call_count: int
    successful_tool_call_count: int
    failed_tool_call_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: float
    safety_violations: int

    def to_dict(self) -> dict[str, object]:
        return {
            "tick_count": self.tick_count,
            "tool_call_count": self.tool_call_count,
            "successful_tool_call_count": self.successful_tool_call_count,
            "failed_tool_call_count": self.failed_tool_call_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": round(self.latency_ms, 3),
            "safety_violations": self.safety_violations,
        }


@dataclass(frozen=True)
class EcommerceRuntimeResult:
    task_id: str
    task_status: str
    success: bool
    root_cause: str | None
    repair_recommendation: str | None
    evidence_ids: tuple[str, ...]
    evidence_sources: tuple[str, ...]
    metrics: EcommerceRuntimeMetrics
    failure_reason: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "task_status": self.task_status,
            "success": self.success,
            "root_cause": self.root_cause,
            "repair_recommendation": self.repair_recommendation,
            "evidence_ids": list(self.evidence_ids),
            "evidence_sources": list(self.evidence_sources),
            "metrics": self.metrics.to_dict(),
            "failure_reason": self.failure_reason,
        }


DecisionEngineFactory = Callable[[], DecisionEngine]


class EcommerceRuntimeService:
    """Run one backend task through the existing Runtime and project metrics."""

    def __init__(
        self,
        adapter: BackendToolAdapter,
        *,
        runtime_factory: Callable[..., AgentRuntime] | None = None,
        store_factory: Callable[[], InMemoryRuntimeStore] = InMemoryRuntimeStore,
    ) -> None:
        self._adapter = adapter
        self._runtime_factory = runtime_factory
        self._store_factory = store_factory

    def run(
        self,
        *,
        goal: str,
        decision_engine: DecisionEngine | None = None,
        decision_engine_factory: DecisionEngineFactory | None = None,
        max_ticks: int = 8,
        profile: TaskProfile = TaskProfile.STANDARD,
        repository_root: str = "ecommerce://adapter",
    ) -> EcommerceRuntimeResult:
        if not goal.strip():
            raise ValueError("goal must be non-empty")
        if decision_engine is not None and decision_engine_factory is not None:
            raise ValueError(
                "provide decision_engine or decision_engine_factory, not both"
            )
        if max_ticks < 1:
            raise ValueError("max_ticks must be positive")

        started = time.perf_counter()
        engine = decision_engine or (
            decision_engine_factory() if decision_engine_factory is not None else None
        )
        if engine is None:
            raise ValueError("a decision engine is required")
        store = self._store_factory()
        task = AgentTask.create(
            goal=goal,
            repository_root=repository_root,
            profile=profile,
        )
        task = replace(task, budget=replace(task.budget, max_ticks=max_ticks))
        store.create_task(task)
        tools = BackendReadOnlyToolCatalog(self._adapter)
        runtime = (
            self._runtime_factory(
                store=store,
                decision_engine=engine,
                tools=tools,
            )
            if self._runtime_factory is not None
            else AgentRuntime(store=store, decision_engine=engine, tools=tools)
        )
        lease_id = f"ecommerce-{uuid4().hex}"
        while not task.status.terminal and task.status is not TaskStatus.WAITING_HUMAN:
            task = runtime.advance(task.task_id, lease_id=lease_id).task

        snapshot = store.snapshot(task.task_id)
        return self._project(snapshot, (time.perf_counter() - started) * 1000)

    @staticmethod
    def _project(
        snapshot: RuntimeSnapshot, latency_ms: float
    ) -> EcommerceRuntimeResult:
        tool_ticks = tuple(
            tick
            for tick in snapshot.ticks
            if tick.decision.kind is DecisionKind.TOOL_CALL
        )
        successful = tuple(
            str(event.payload.get("tool_name") or "")
            for event in snapshot.events
            if event.kind == "tool.succeeded"
        )
        rejected = tuple(
            str(event.payload.get("reason_code") or "")
            for event in snapshot.events
            if event.kind == "tool.rejected"
        )
        input_tokens = sum(item.actual_input_tokens for item in snapshot.usage)
        output_tokens = sum(item.actual_output_tokens for item in snapshot.usage)
        safety_codes = {
            "UNKNOWN_TOOL",
            "WRITE_OPERATION_FORBIDDEN",
            "TOOL_NOT_ALLOWED",
            "PATH_OUT_OF_SCOPE",
            "TENANT_SCOPE_VIOLATION",
        }
        safety_violations = len(set(rejected).intersection(safety_codes))
        report = snapshot.task.final_report
        failure_reason = None
        if snapshot.task.status is not TaskStatus.SUCCEEDED:
            failure_reason = rejected[-1] if rejected else snapshot.task.status.value
        return EcommerceRuntimeResult(
            task_id=snapshot.task.task_id,
            task_status=snapshot.task.status.value,
            success=snapshot.task.status is TaskStatus.SUCCEEDED,
            root_cause=report.root_cause if report else None,
            repair_recommendation=report.repair_recommendation if report else None,
            evidence_ids=tuple(item.evidence_id for item in snapshot.evidence),
            evidence_sources=tuple(item.source for item in snapshot.evidence),
            metrics=EcommerceRuntimeMetrics(
                tick_count=len(snapshot.ticks),
                tool_call_count=len(tool_ticks),
                successful_tool_call_count=len(successful),
                failed_tool_call_count=len(rejected),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                latency_ms=latency_ms,
                safety_violations=safety_violations,
            ),
            failure_reason=failure_reason,
        )


__all__ = [
    "EcommerceRuntimeMetrics",
    "EcommerceRuntimeResult",
    "EcommerceRuntimeService",
    "SequenceDecisionEngine",
]
