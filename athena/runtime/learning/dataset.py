"""Training-ready dataset construction from verified Runtime trajectories.

This module is deliberately a data-governance boundary, not a model trainer.
It converts successful, operator-verified Runtime snapshots into bounded
examples for offline evaluation or later fine-tuning. Raw Artifacts, secrets,
and hidden reasoning are never part of an example.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from athena.runtime.models import DecisionKind, RuntimeSnapshot, TaskStatus

from .lifecycle import redact_summary
from .models import OperatorFeedback


class DatasetSplit(StrEnum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


@dataclass(frozen=True)
class DatasetExample:
    """One bounded supervised example produced from a verified trajectory."""

    example_id: str
    source_task_id: str
    workflow_type: str
    split: DatasetSplit
    input: dict[str, object]
    target: dict[str, object]
    provenance: dict[str, object]
    quality: dict[str, object]

    def to_training_record(self) -> dict[str, object]:
        """Return a provider-neutral chat/instruction training record."""

        return {
            "schema_version": "runtime.training.example.v1",
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        self.input,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
                {
                    "role": "assistant",
                    "content": json.dumps(
                        self.target,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            ],
            "metadata": {
                "example_id": self.example_id,
                "source_task_id": self.source_task_id,
                "workflow_type": self.workflow_type,
                "split": self.split.value,
                "quality": dict(self.quality),
                "provenance": dict(self.provenance),
            },
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "example_id": self.example_id,
            "source_task_id": self.source_task_id,
            "workflow_type": self.workflow_type,
            "split": self.split.value,
            "input": dict(self.input),
            "target": dict(self.target),
            "provenance": dict(self.provenance),
            "quality": dict(self.quality),
        }


@dataclass(frozen=True)
class DatasetBuildReport:
    """Auditable result of one dataset construction pass."""

    dataset_id: str
    examples: tuple[DatasetExample, ...]
    rejected: tuple[dict[str, object], ...]
    duplicate_count: int
    redaction_count: int

    @property
    def split_counts(self) -> dict[str, int]:
        return {
            split.value: sum(item.split is split for item in self.examples)
            for split in DatasetSplit
        }

    @property
    def quality_scores(self) -> tuple[float, ...]:
        return tuple(float(item.quality["score"]) for item in self.examples)

    @property
    def average_quality_score(self) -> float:
        if not self.quality_scores:
            return 0.0
        return round(sum(self.quality_scores) / len(self.quality_scores), 4)

    @property
    def min_quality_score(self) -> float:
        return round(min(self.quality_scores), 4) if self.quality_scores else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "schema_version": "runtime.training.dataset.v1",
            "example_count": len(self.examples),
            "rejected_count": len(self.rejected),
            "duplicate_count": self.duplicate_count,
            "redaction_count": self.redaction_count,
            "split_counts": self.split_counts,
            "average_quality_score": self.average_quality_score,
            "min_quality_score": self.min_quality_score,
            "rejected": [dict(item) for item in self.rejected],
            "examples": [item.to_dict() for item in self.examples],
        }

    def to_jsonl(self) -> str:
        return "\n".join(
            json.dumps(
                item.to_training_record(),
                ensure_ascii=False,
                sort_keys=True,
            )
            for item in self.examples
        ) + ("\n" if self.examples else "")


class TrajectoryDatasetBuilder:
    """Build a deterministic, redacted, deduplicated trajectory dataset."""

    def __init__(
        self,
        *,
        min_evidence: int = 3,
        validation_ratio: float = 0.10,
        test_ratio: float = 0.10,
    ) -> None:
        if min_evidence < 1:
            raise ValueError("min_evidence must be at least one")
        if validation_ratio < 0 or test_ratio < 0:
            raise ValueError("dataset split ratios must be non-negative")
        if validation_ratio + test_ratio >= 1:
            raise ValueError("validation and test ratios must leave a train split")
        self._min_evidence = min_evidence
        self._validation_ratio = validation_ratio
        self._test_ratio = test_ratio

    def build(
        self,
        trajectories: Iterable[tuple[RuntimeSnapshot, OperatorFeedback]],
    ) -> DatasetBuildReport:
        examples: list[DatasetExample] = []
        rejected: list[dict[str, object]] = []
        fingerprints: set[str] = set()
        redaction_count = 0
        duplicate_count = 0

        for snapshot, feedback in trajectories:
            result = self._build_example(snapshot, feedback)
            if result[0] is None:
                rejected.append(
                    {
                        "task_id": snapshot.task.task_id,
                        "reason_code": result[1],
                    }
                )
                continue
            example, fingerprint, redactions = result
            redaction_count += redactions
            if fingerprint in fingerprints:
                duplicate_count += 1
                continue
            fingerprints.add(fingerprint)
            examples.append(example)

        dataset_digest = hashlib.sha256(
            json.dumps(
                sorted(item.example_id for item in examples),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
        return DatasetBuildReport(
            dataset_id=f"runtime-dataset-{dataset_digest}",
            examples=tuple(examples),
            rejected=tuple(rejected),
            duplicate_count=duplicate_count,
            redaction_count=redaction_count,
        )

    def _build_example(
        self,
        snapshot: RuntimeSnapshot,
        feedback: OperatorFeedback,
    ) -> tuple[DatasetExample | None, str | None, int]:
        task = snapshot.task
        if task.status is not TaskStatus.SUCCEEDED or task.final_report is None:
            return None, "TASK_NOT_SUCCEEDED", 0
        if not feedback.verified or not feedback.accepted:
            return None, "VERIFIED_OPERATOR_FEEDBACK_REQUIRED", 0
        if not feedback.feedback_id.strip():
            return None, "FEEDBACK_ID_REQUIRED", 0

        evidence_by_id = {item.evidence_id: item for item in snapshot.evidence}
        evidence_ids = tuple(dict.fromkeys(task.final_report.evidence_ids))
        if len(evidence_ids) < self._min_evidence:
            return None, "EVIDENCE_THRESHOLD_NOT_MET", 0
        if any(item not in evidence_by_id for item in evidence_ids):
            return None, "EVIDENCE_REFERENCE_UNVERIFIED", 0

        tool_sequence = tuple(
            {
                "sequence": tick.sequence,
                "tool_name": tick.decision.tool_name,
                "reason_code": tick.decision.reason_code,
                "status": tick.status.value,
            }
            for tick in snapshot.ticks
            if tick.decision.kind is DecisionKind.TOOL_CALL
        )
        if not tool_sequence:
            return None, "TOOL_SEQUENCE_REQUIRED", 0

        try:
            goal = redact_summary(task.goal, limit=2_000)
            root_cause = redact_summary(task.final_report.root_cause)
            recommendation = redact_summary(task.final_report.repair_recommendation)
            feedback_summary = redact_summary(feedback.summary)
            evidence_refs = tuple(
                {
                    "evidence_id": item,
                    "source": evidence_by_id[item].source,
                    "summary": redact_summary(evidence_by_id[item].summary),
                }
                for item in evidence_ids
            )
        except Exception:
            return None, "PUBLIC_SUMMARY_REQUIRED", 0

        input_payload: dict[str, object] = {
            "task_goal": goal,
            "task_profile": task.profile.value,
            # Runtime IDs remain audit metadata; the model sees only stable
            # evidence semantics rather than learning per-run UUIDs.
            "evidence_refs": [
                {"source": item["source"], "summary": item["summary"]}
                for item in evidence_refs
            ],
            "constraints": [
                "readonly_tools_only",
                "evidence_backed_conclusion",
                "no_hidden_reasoning",
            ],
        }
        target_payload: dict[str, object] = {
            "tool_sequence": list(tool_sequence),
            "root_cause": root_cause,
            "repair_recommendation": recommendation,
            "termination": "succeeded",
        }
        fingerprint_payload = {
            "workflow_type": "repository_diagnosis",
            "task_goal": goal,
            "evidence": [
                {"source": item["source"], "summary": item["summary"]}
                for item in evidence_refs
            ],
            "target": target_payload,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        redactions = sum(
            value.count("[REDACTED_")
            for value in (
                goal,
                root_cause,
                recommendation,
                feedback_summary,
                *(item["summary"] for item in evidence_refs),
            )
        )
        quality = {
            "score": 1.0,
            "verified_feedback": True,
            "evidence_count": len(evidence_refs),
            "tool_count": len(tool_sequence),
            "redaction_count": redactions,
            "raw_artifacts_included": False,
            "hidden_reasoning_included": False,
            "operator_feedback_summary": feedback_summary,
        }
        example_digest = fingerprint[:24]
        example = DatasetExample(
            example_id=f"runtime-training-example-{example_digest}",
            source_task_id=task.task_id,
            workflow_type="repository_diagnosis",
            split=self._split(fingerprint),
            input=input_payload,
            target=target_payload,
            provenance={
                "source_type": "verified_runtime_trajectory",
                "source_task_id": task.task_id,
                "feedback_id": feedback.feedback_id.strip(),
                "evidence_ids": list(evidence_ids),
                "raw_artifacts_included": False,
                "hidden_reasoning_included": False,
            },
            quality=quality,
        )
        return example, fingerprint, redactions

    def _split(self, fingerprint: str) -> DatasetSplit:
        bucket = int(fingerprint[:8], 16) / 0xFFFFFFFF
        if bucket >= 1 - self._test_ratio:
            return DatasetSplit.TEST
        if bucket >= 1 - self._test_ratio - self._validation_ratio:
            return DatasetSplit.VALIDATION
        return DatasetSplit.TRAIN


__all__ = [
    "DatasetBuildReport",
    "DatasetExample",
    "DatasetSplit",
    "TrajectoryDatasetBuilder",
]
