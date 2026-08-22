"""Reproducible human-baseline versus Agent Runtime productivity study."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from athena.application.ecommerce_runtime import (
    EcommerceRuntimeResult,
    EcommerceRuntimeService,
    SequenceDecisionEngine,
)
from athena.backend import (
    EVENT_TOOL,
    INVENTORY_TOOL,
    LOG_TOOL,
    METRIC_TOOL,
    ORDER_TOOL,
    PAYMENT_TOOL,
    BackendQuery,
    BackendToolAdapter,
)
from athena.runtime import Decision, DecisionKind


@dataclass(frozen=True)
class EcommerceProductivityCase:
    case_id: str
    category: str
    goal: str
    queries: tuple[tuple[str, dict[str, Any]], ...]
    expected_success: bool = True


@dataclass(frozen=True)
class ProductivityMetrics:
    task_success: bool
    evidence_retention: float
    tick_count: int
    tool_call_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: float
    safety_violations: int
    failure_reason: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "task_success": self.task_success,
            "evidence_retention": round(self.evidence_retention, 4),
            "tick_count": self.tick_count,
            "tool_call_count": self.tool_call_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": round(self.latency_ms, 3),
            "safety_violations": self.safety_violations,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True)
class ProductivityComparison:
    case_id: str
    category: str
    baseline: ProductivityMetrics
    agent: ProductivityMetrics

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "baseline": self.baseline.to_dict(),
            "agent": self.agent.to_dict(),
        }


@dataclass(frozen=True)
class ProductivityReport:
    case_count: int
    comparisons: tuple[ProductivityComparison, ...]
    aggregate: dict[str, object]
    claims_scope: str = "offline_deterministic_fixture_only"

    def to_dict(self) -> dict[str, object]:
        return {
            "case_count": self.case_count,
            "comparisons": [item.to_dict() for item in self.comparisons],
            "aggregate": self.aggregate,
            "claims_scope": self.claims_scope,
        }


def default_productivity_cases() -> tuple[EcommerceProductivityCase, ...]:
    """Return four fixed backend workflows with no network dependency."""

    return (
        EcommerceProductivityCase(
            "paid-not-fulfilled",
            "fulfillment",
            "Diagnose why ord-1001 is paid but fulfillment is pending.",
            (
                (ORDER_TOOL, {"order_id": "ord-1001"}),
                (PAYMENT_TOOL, {"order_id": "ord-1001"}),
                (EVENT_TOOL, {"order_id": "ord-1001"}),
                (LOG_TOOL, {"service": "order-service", "query": "outbox"}),
                (
                    METRIC_TOOL,
                    {"service": "order-service", "metric": "outbox_publish_errors"},
                ),
            ),
        ),
        EcommerceProductivityCase(
            "inventory-shortage",
            "inventory",
            "Diagnose whether inventory blocked ord-1001 fulfillment.",
            (
                (ORDER_TOOL, {"order_id": "ord-1001"}),
                (INVENTORY_TOOL, {"sku_id": "sku-100", "warehouse_id": "wh-east"}),
            ),
        ),
        EcommerceProductivityCase(
            "payment-order-consistency",
            "payment",
            "Check payment and order consistency for ord-1002.",
            (
                (PAYMENT_TOOL, {"order_id": "ord-1002"}),
                (ORDER_TOOL, {"order_id": "ord-1002"}),
            ),
        ),
        EcommerceProductivityCase(
            "missing-order",
            "failure-recovery",
            "Diagnose a missing order without inventing evidence.",
            ((ORDER_TOOL, {"order_id": "missing-order"}),),
            expected_success=False,
        ),
    )


def run_productivity_study(
    adapter: BackendToolAdapter,
    *,
    cases: tuple[EcommerceProductivityCase, ...] | None = None,
) -> ProductivityReport:
    """Run both sides using explicit, inspectable protocols.

    The human baseline executes the fixed query checklist directly. The Agent
    side executes the same checklist as Decisions through the real Runtime.
    """

    selected = cases or default_productivity_cases()
    comparisons = tuple(_compare_case(adapter, case) for case in selected)
    return ProductivityReport(
        case_count=len(comparisons),
        comparisons=comparisons,
        aggregate=_aggregate(comparisons),
    )


def _compare_case(
    adapter: BackendToolAdapter, case: EcommerceProductivityCase
) -> ProductivityComparison:
    baseline = _run_human_baseline(adapter, case)
    started = time.perf_counter()
    decisions = tuple(
        Decision(
            kind=DecisionKind.TOOL_CALL,
            reason_code="PRODUCTIVITY_CHECKLIST_STEP",
            tool_name=name,
            arguments=arguments,
        )
        for name, arguments in case.queries
    )
    if case.expected_success:
        decisions += (
            Decision(
                kind=DecisionKind.FINAL,
                reason_code="PRODUCTIVITY_EVIDENCE_SUFFICIENT",
                response="Summarize retained evidence.",
            ),
        )
    agent_result = EcommerceRuntimeService(adapter).run(
        goal=case.goal,
        decision_engine=SequenceDecisionEngine(decisions),
        max_ticks=len(decisions),
    )
    agent = _agent_metrics(agent_result, (time.perf_counter() - started) * 1000)
    return ProductivityComparison(case.case_id, case.category, baseline, agent)


def _run_human_baseline(
    adapter: BackendToolAdapter, case: EcommerceProductivityCase
) -> ProductivityMetrics:
    started = time.perf_counter()
    successful = 0
    failure_reason: str | None = None
    for tool_name, arguments in case.queries:
        result = adapter.query(BackendQuery(tool_name, arguments))
        if not result.success:
            failure_reason = (
                result.error_code.value if result.error_code else "QUERY_FAILED"
            )
            break
        successful += 1
    return ProductivityMetrics(
        task_success=successful == len(case.queries) and case.expected_success,
        evidence_retention=successful / len(case.queries),
        tick_count=successful,
        tool_call_count=successful,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        latency_ms=(time.perf_counter() - started) * 1000,
        safety_violations=0,
        failure_reason=failure_reason,
    )


def _agent_metrics(
    result: EcommerceRuntimeResult, latency_ms: float
) -> ProductivityMetrics:
    metrics = result.metrics
    expected = max(metrics.tool_call_count, 1)
    return ProductivityMetrics(
        task_success=result.success,
        evidence_retention=len(result.evidence_ids) / expected,
        tick_count=metrics.tick_count,
        tool_call_count=metrics.tool_call_count,
        input_tokens=metrics.input_tokens,
        output_tokens=metrics.output_tokens,
        total_tokens=metrics.total_tokens,
        latency_ms=latency_ms,
        safety_violations=metrics.safety_violations,
        failure_reason=result.failure_reason,
    )


def _aggregate(
    comparisons: tuple[ProductivityComparison, ...],
) -> dict[str, object]:
    def average(side: str, field: str) -> float:
        values = [getattr(getattr(item, side), field) for item in comparisons]
        return round(sum(values) / len(values), 4) if values else 0.0

    def success_rate(side: str) -> float:
        return round(
            sum(bool(getattr(item, side).task_success) for item in comparisons)
            / len(comparisons),
            4,
        )

    fields = (
        "evidence_retention",
        "tick_count",
        "tool_call_count",
        "input_tokens",
        "total_tokens",
        "latency_ms",
        "safety_violations",
    )
    baseline = {field: average("baseline", field) for field in fields}
    agent = {field: average("agent", field) for field in fields}
    delta = {field: round(agent[field] - baseline[field], 4) for field in fields}
    return {
        "baseline_success_rate": success_rate("baseline"),
        "agent_success_rate": success_rate("agent"),
        "baseline": baseline,
        "agent": agent,
        "delta": delta,
    }


__all__ = [
    "EcommerceProductivityCase",
    "ProductivityComparison",
    "ProductivityMetrics",
    "ProductivityReport",
    "default_productivity_cases",
    "run_productivity_study",
]
