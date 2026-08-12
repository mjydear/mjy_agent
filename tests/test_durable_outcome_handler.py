from __future__ import annotations

from types import SimpleNamespace

import pytest

from athena.application.diagnosis_outcome_service import DiagnosisOutcomeServiceError
from athena.application.durable_outcome_handler import DurableOutcomeRecordingHandler
from athena.application.durable_worker import WorkerOutcome


def _task() -> SimpleNamespace:
    return SimpleNamespace(tenant_id="tenant-a", task_id="task-1")


class RecordingOutcomeService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def finalize(self, tenant_id: str, task_id: str, **kwargs: object):
        self.calls.append({"tenant_id": tenant_id, "task_id": task_id, **kwargs})
        return SimpleNamespace(outcome_id="outcome-1", evidence_sufficient=True)


class FailingOutcomeService:
    async def finalize(self, tenant_id: str, task_id: str, **kwargs: object):
        raise DiagnosisOutcomeServiceError("OUTCOME_EVIDENCE_NOT_FOUND", "missing")


@pytest.mark.asyncio
async def test_successful_readonly_handler_is_checkpointed_only_after_outcome() -> None:
    outcomes = RecordingOutcomeService()

    async def delegate(task: object) -> WorkerOutcome:
        return WorkerOutcome(
            state={
                "evidence_ids": ["evidence-1"],
                "root_causes": [
                    {"root_cause": "image pull failed", "severity": "high"}
                ],
                "readonly_report": {"actions": ["check imagePullSecret"]},
            }
        )

    result = await DurableOutcomeRecordingHandler(delegate, outcomes)(_task())

    assert result.retry_delay_seconds is None
    assert result.state["diagnosis_outcome_id"] == "outcome-1"
    assert outcomes.calls[0]["supporting_evidence_ids"] == ("evidence-1",)
    assert outcomes.calls[0]["evidence_sufficient"] is True
    assert "thought" not in str(outcomes.calls[0]).lower()


@pytest.mark.asyncio
async def test_outcome_persistence_failure_uses_worker_retry_contract() -> None:
    async def delegate(task: object) -> WorkerOutcome:
        return WorkerOutcome(
            state={
                "evidence_ids": ["evidence-1"],
                "root_causes": [{"root_cause": "supported cause"}],
                "readonly_report": {"actions": ["observe"]},
            }
        )

    result = await DurableOutcomeRecordingHandler(
        delegate, FailingOutcomeService(), retry_delay_seconds=2.0
    )(_task())

    assert result.status == "failed"
    assert result.retry_delay_seconds == 2.0
    assert result.error_code == "DIAGNOSIS_OUTCOME_PERSIST_FAILED"
    assert "missing" not in str(result.state)


@pytest.mark.asyncio
async def test_delegate_retry_or_failure_does_not_create_outcome() -> None:
    outcomes = RecordingOutcomeService()

    async def delegate(task: object) -> WorkerOutcome:
        return WorkerOutcome(
            state={"thought": "never durable"},
            status="failed",
            retry_delay_seconds=1.0,
            error_code="K8S_TIMEOUT",
        )

    result = await DurableOutcomeRecordingHandler(delegate, outcomes)(_task())

    assert result.error_code == "K8S_TIMEOUT"
    assert outcomes.calls == []
