"""Unit tests for Phase 0 OpsTask, Evidence, and Tool V2 contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from athena.agent.policy.contracts import (
    ActionDecision,
    DataOrigin,
    EnvironmentMode,
    ExecutionProfile,
    RiskLevel,
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
from athena.memory.evidence import Evidence


def _state() -> OpsTaskState:
    return OpsTaskState(
        task_id="task-1",
        tenant_id="tenant-a",
        objective="diagnose CrashLoopBackOff",
        environment_id="env-1",
        environment_mode=EnvironmentMode.LIVE,
        scope={"namespace": "payment"},
        tenant_policy_snapshot={"readonly": True},
        budget=TaskBudget(
            remaining_steps=5, remaining_tokens=6000, remaining_time_ms=30000
        ),
        execution_profile=ExecutionProfile.BOUNDED_POLICY_LOOP,
    )


def test_action_decision_rejects_invalid_confidence() -> None:
    with pytest.raises(ValueError):
        ActionDecision(
            action="k8s.logs.read", reason_code="LOG_REQUIRED", confidence=1.1
        )


def test_tool_v2_contracts_require_tenant_and_consistent_results() -> None:
    with pytest.raises(ValueError):
        ToolCallV2(
            call_id="call-1", task_id="task-1", tenant_id="", tool_name="k8s.logs.read"
        )
    with pytest.raises(ValueError):
        ToolResultV2(
            status=ToolStatus.FAILED,
            summary="provider unavailable",
            data=None,
        )

    spec = ToolSpecV2(
        name="k8s.logs.read",
        version="1.0.0",
        domain="kubernetes",
        input_schema={},
        output_schema={},
        required_capabilities=("k8s.logs.read",),
        risk_level=RiskLevel.S1,
        readonly=True,
        idempotent=True,
        timeout_seconds=10,
    )
    assert spec.readonly is True


def test_evidence_requires_tenant_origin_and_ordered_timestamps() -> None:
    observed = datetime.now(UTC)
    evidence = Evidence(
        id="ev-1",
        tenant_id="tenant-a",
        task_id="task-1",
        type="log",
        source="k8s.logs.read",
        data_origin=DataOrigin.LIVE,
        summary="connection refused",
        content_ref="evidence://ev-1",
        content_hash="sha256:abc",
        observed_at=observed,
        collected_at=observed + timedelta(seconds=1),
    )
    assert evidence.data_origin is DataOrigin.LIVE

    with pytest.raises(ValueError):
        Evidence(
            id="ev-2",
            tenant_id="",
            task_id="task-1",
            type="log",
            source="k8s.logs.read",
            data_origin=DataOrigin.LIVE,
            summary="x",
            content_ref=None,
            content_hash="sha256:def",
            observed_at=observed,
            collected_at=observed,
        )


def test_task_state_transition_is_versioned_and_terminal_states_are_closed() -> None:
    state = _state()
    running = state.transition_to(OpsTaskStatus.RUNNING, OpsTaskPhase.COLLECT)
    assert running.state_version == 1
    assert running.phase is OpsTaskPhase.COLLECT

    completed = running.transition_to(OpsTaskStatus.SUCCEEDED, OpsTaskPhase.REPORT)
    with pytest.raises(ValueError):
        completed.transition_to(OpsTaskStatus.RUNNING)
