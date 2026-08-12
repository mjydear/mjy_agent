"""Tests for durable diagnosis workflow routing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from athena.application.durable_worker import WorkerOutcome
from athena.application.diagnosis_handler_registry import (
    SUPPORTED_DIAGNOSIS_WORKFLOWS,
    DiagnosisHandlerRegistry,
)
from athena.application.worker_runtime import build_diagnosis_handler_registry
from athena.config import AthenaSettings


class MarkerHandler:
    def __init__(self, marker: str) -> None:
        self.marker = marker

    async def __call__(self, task: object) -> WorkerOutcome:
        return WorkerOutcome(state={"handler": self.marker})


def _task(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "objective": "diagnose pod anomaly",
        "scope": {"namespace": "default"},
        "state": {},
        "config_snapshot": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _registry() -> tuple[DiagnosisHandlerRegistry, dict[str, MarkerHandler]]:
    handlers = {
        workflow: MarkerHandler(workflow)
        for workflow in SUPPORTED_DIAGNOSIS_WORKFLOWS
    }
    return DiagnosisHandlerRegistry(handlers), handlers


@pytest.mark.parametrize("workflow", SUPPORTED_DIAGNOSIS_WORKFLOWS)
def test_explicit_workflow_type_routes_to_its_handler(workflow: str) -> None:
    registry, handlers = _registry()

    assert registry.resolve(_task(workflow_type=workflow)) is handlers[workflow]


def test_explicit_workflow_type_wins_over_conflicting_alert_hint() -> None:
    registry, handlers = _registry()

    task = _task(
        workflow_type="image_pull",
        objective="diagnose KubePodCrashLooping",
        scope={"namespace": "default", "alert_name": "KubePodCrashLooping"},
    )

    assert registry.resolve(task) is handlers["image_pull"]


@pytest.mark.parametrize(
    ("alert_name", "expected"),
    [
        ("KubePodCrashLooping", "crashloop"),
        ("KubePodPending", "pod_pending"),
        ("KubePodImagePullBackOff", "image_pull"),
        ("KubePodResourcePressure", "resource_pressure"),
    ],
)
def test_alert_name_scope_fallback_routes_legacy_persisted_tasks(
    alert_name: str, expected: str
) -> None:
    registry, handlers = _registry()

    task = _task(scope={"namespace": "default", "alert_name": alert_name})

    assert registry.resolve(task) is handlers[expected]


@pytest.mark.asyncio
async def test_unknown_workflow_returns_terminal_non_retryable_failure() -> None:
    registry, _ = _registry()

    outcome = await registry(
        _task(
            workflow_type="unsupported",
            scope={"namespace": "default", "alert_name": "KubePodCrashLooping"},
        )
    )

    assert outcome.status == "failed"
    assert outcome.phase == "report"
    assert outcome.event_type == "task.failed"
    assert outcome.retry_delay_seconds is None
    assert outcome.error_code == "UNSUPPORTED_DIAGNOSIS_WORKFLOW"
    assert outcome.state["error_code"] == "UNSUPPORTED_DIAGNOSIS_WORKFLOW"


@pytest.mark.asyncio
async def test_unknown_alert_hint_does_not_fall_back_to_crashloop() -> None:
    registry, _ = _registry()

    outcome = await registry(
        _task(scope={"namespace": "default", "alert_name": "KubePodUnknown"})
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "UNSUPPORTED_DIAGNOSIS_WORKFLOW"
    assert outcome.retry_delay_seconds is None


@pytest.mark.asyncio
async def test_registry_preserves_existing_async_task_handler_call_contract() -> None:
    registry, _ = _registry()

    outcome = await registry(_task(workflow_type="crashloop"))

    assert outcome.state == {"handler": "crashloop"}


def test_runtime_registry_construction_keeps_all_pod_routes() -> None:
    registry = build_diagnosis_handler_registry(AthenaSettings(), None)

    assert registry.supported_workflows == SUPPORTED_DIAGNOSIS_WORKFLOWS
