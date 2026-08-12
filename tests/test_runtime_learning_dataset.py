"""Dataset construction contracts for Runtime self-evolution."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

from athena.runtime import AgentRuntime, AgentTask, InMemoryRuntimeStore, TaskStatus
from athena.runtime.learning import (
    OperatorFeedback,
    TrajectoryDatasetBuilder,
)


def _completed_snapshot():
    store = InMemoryRuntimeStore()
    task = AgentTask.create(
        goal="Diagnose the pricing calculation failure",
        repository_root=str(Path(__file__).parent / "fixtures" / "runtime_repo"),
    )
    store.create_task(task)
    runtime = AgentRuntime(store=store)
    for _ in range(4):
        runtime.advance(task.task_id, lease_id="dataset-test-worker")
    snapshot = store.snapshot(task.task_id)
    assert snapshot.task.status is TaskStatus.SUCCEEDED
    return snapshot


def _feedback(*, accepted: bool = True) -> OperatorFeedback:
    return OperatorFeedback(
        feedback_id="dataset-feedback-1",
        accepted=accepted,
        verified=True,
        summary="Operator verified the result. api_key=must-redact hidden thought=must-redact.",
        submitted_by="operator-a",
    )


def test_builder_creates_redacted_training_record_without_raw_artifacts() -> None:
    snapshot = _completed_snapshot()
    unsafe = replace(
        snapshot.evidence[0],
        summary="API_KEY=raw-secret hidden thought: do not persist this",
    )
    snapshot = replace(snapshot, evidence=(unsafe, *snapshot.evidence[1:]))

    report = TrajectoryDatasetBuilder().build(((snapshot, _feedback()),))

    assert len(report.examples) == 1
    example = report.examples[0]
    assert example.quality["raw_artifacts_included"] is False
    assert example.quality["hidden_reasoning_included"] is False
    assert example.quality["redaction_count"] >= 2
    assert all("evidence_id" not in item for item in example.input["evidence_refs"])
    assert example.provenance["evidence_ids"]
    serialized = report.to_jsonl()
    records = [json.loads(line) for line in serialized.splitlines()]
    assert len(records) == 1
    model_input = json.loads(records[0]["messages"][0]["content"])
    assert all("evidence_id" not in item for item in model_input["evidence_refs"])
    assert "raw-secret" not in serialized
    assert "hidden thought" not in serialized.lower()
    assert "irrelevant diagnostic trace" not in serialized
    assert "tool_sequence" in serialized


def test_builder_deduplicates_semantically_identical_trajectories_and_rejects_bad_feedback() -> None:
    snapshot = _completed_snapshot()
    builder = TrajectoryDatasetBuilder()

    report = builder.build(
        (
            (snapshot, _feedback()),
            (snapshot, replace(_feedback(), feedback_id="dataset-feedback-2")),
            (snapshot, _feedback(accepted=False)),
        )
    )

    assert len(report.examples) == 1
    assert report.duplicate_count == 1
    assert len(report.rejected) == 1
    assert report.rejected[0]["reason_code"] == "VERIFIED_OPERATOR_FEEDBACK_REQUIRED"
    assert (
        report.split_counts["train"]
        + report.split_counts["validation"]
        + report.split_counts["test"]
        == 1
    )
