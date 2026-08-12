"""P5-01 capability bundle and second readonly workflow tests."""

from __future__ import annotations

import pytest

from athena.agent.context.manager import DecisionContext
from athena.agent.policy.contracts import (
    ActionDecision,
    EnvironmentMode,
    ExecutionProfile,
    ToolResultV2,
    ToolStatus,
)
from athena.agent.workflow.pod_pending import (
    PodPendingDiagnosisWorkflow,
    PodPendingEscalation,
)
from athena.agent.workflow.state import OpsTaskState, TaskBudget
from athena.capabilities import default_capability_registry


def _context(facts: list[dict[str, object]] | None = None) -> DecisionContext:
    return DecisionContext(
        task_id="ops-pending-1",
        tenant_id="tenant-a",
        available_actions=("k8s.pod.list", "k8s.pod.get", "k8s.events.list"),
        payload={
            "task": {"scope": {"namespace": "payment"}},
            "verified_facts": facts or [],
        },
        estimated_tokens=100,
        compression_metrics={},
    )


def _state(*facts: dict[str, object]) -> OpsTaskState:
    return OpsTaskState(
        task_id="ops-pending-1",
        tenant_id="tenant-a",
        objective="diagnose pending pod",
        environment_id="env-a",
        environment_mode=EnvironmentMode.MOCK,
        scope={"namespace": "payment"},
        tenant_policy_snapshot={"readonly": True},
        budget=TaskBudget(
            remaining_steps=3, remaining_tokens=3000, remaining_time_ms=10000
        ),
        execution_profile=ExecutionProfile.BOUNDED_POLICY_LOOP,
        facts=tuple(dict(fact) for fact in facts),
    )


def test_kubernetes_readonly_bundle_selects_without_runner_changes() -> None:
    registry = default_capability_registry()
    selected = registry.select_for(frozenset({"k8s.workload.read", "k8s.events.read"}))

    assert len(selected) == 1
    bundle = selected[0]
    assert bundle.bundle_id == "kubernetes-readonly"
    assert "pod_pending" in bundle.workflows
    assert {spec.name for spec in bundle.tool_specs} >= {
        "k8s.pod.list",
        "k8s.pod.get",
        "k8s.events.list",
    }
    assert all(spec.readonly for spec in bundle.tool_specs)


def test_pod_pending_workflow_rules_collect_pods_then_details_then_events() -> None:
    workflow = PodPendingDiagnosisWorkflow()

    first = workflow.rules_only_decision(_context())
    assert first == ActionDecision(
        action="k8s.pod.list",
        arguments={"namespace": "payment"},
        reason_code="RULES_COLLECT_PENDING_PODS",
        confidence=1.0,
    )

    second = workflow.rules_only_decision(
        _context(
            [
                {
                    "action": "k8s.pod.list",
                    "pending_pods": ["api-0"],
                    "data_origin": "mock",
                }
            ]
        )
    )
    assert second.action == "k8s.pod.get"
    assert second.arguments == {"namespace": "payment", "name": "api-0"}

    third = workflow.rules_only_decision(
        _context(
            [
                {"action": "k8s.pod.list", "pending_pods": ["api-0"]},
                {"action": "k8s.pod.get", "pending_observed": True},
            ]
        )
    )
    assert third.action == "k8s.events.list"
    assert third.arguments == {"namespace": "payment", "pod_name": "api-0"}


def test_pod_pending_workflow_rejects_missing_pending_evidence() -> None:
    workflow = PodPendingDiagnosisWorkflow()
    with pytest.raises(PodPendingEscalation) as exc:
        workflow.rules_only_decision(
            _context([{"action": "k8s.pod.list", "pending_pods": []}])
        )
    assert exc.value.error_code == "POD_PENDING_NOT_OBSERVED"


def test_pod_pending_workflow_derives_terminal_root_cause_from_events() -> None:
    workflow = PodPendingDiagnosisWorkflow()
    decision = ActionDecision(
        action="k8s.events.list",
        arguments={"namespace": "payment", "pod_name": "api-0"},
        reason_code="RULES_COLLECT_PENDING_EVENTS",
    )
    fact = workflow.fact_from_result(
        decision,
        ToolResultV2(
            status=ToolStatus.SUCCEEDED,
            summary="events collected",
            data={
                "items": [
                    {
                        "reason": "FailedScheduling",
                        "message": "0/3 nodes are available",
                    }
                ],
                "data_origin": "mock",
            },
            evidence_refs=("evidence-1",),
        ),
    )

    assert fact is not None
    assert fact["root_cause"] == "Scheduler could not place the pod"
    state = _state(fact)
    assert workflow.is_complete(state) is True
    assert workflow.terminal_error(state) is None


def test_pod_pending_workflow_fails_on_origin_mismatch_or_missing_cause() -> None:
    workflow = PodPendingDiagnosisWorkflow()
    assert (
        workflow.terminal_error(
            _state(
                {
                    "action": "k8s.events.list",
                    "event_reasons": ["FailedScheduling"],
                    "root_cause": "Scheduler could not place the pod",
                    "evidence_ids": ["evidence-1"],
                    "data_origin": "live",
                }
            )
        )
        == "EVIDENCE_ORIGIN_MISMATCH"
    )
    assert (
        workflow.terminal_error(
            _state(
                {
                    "action": "k8s.events.list",
                    "event_reasons": ["Pulled"],
                    "evidence_ids": ["evidence-1"],
                    "data_origin": "mock",
                }
            )
        )
        == "WORKFLOW_ESCALATION_REQUIRED"
    )
