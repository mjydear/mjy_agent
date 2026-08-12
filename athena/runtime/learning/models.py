"""Governed, runtime-local Skill learning domain objects.

The runtime observes completed tasks but never turns their output directly into
an executable or active Skill.  This module intentionally stores only bounded,
redacted summaries and Evidence references.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from athena.learning.skill_candidate import (
    CANDIDATE_STATUS,
    REJECTED_STATUS,
    REPLAY_PENDING_STATUS,
    REVIEW_PENDING_STATUS,
    SHADOW_STATUS,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class RuntimeSkillLearningError(RuntimeError):
    """A stable domain error for an invalid runtime learning transition."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class OperatorFeedback:
    """A verified human assessment of a completed runtime task."""

    feedback_id: str
    accepted: bool
    verified: bool
    summary: str
    submitted_by: str


@dataclass(frozen=True)
class RuntimeSkillCandidate:
    """An offline candidate with no activation or execution capability."""

    candidate_id: str
    name: str
    workflow_type: str
    environment_type: str
    capabilities: tuple[str, ...]
    status: str
    source_task_id: str
    source_evidence_ids: tuple[str, ...]
    feedback_id: str
    manifest: dict[str, object]
    procedure: dict[str, object]
    source_summary: dict[str, object]
    audit_events: tuple[dict[str, object], ...]
    replay_report_id: str | None = None
    shadow_report_id: str | None = None
    reviewed_by: str | None = None
    review_note: str | None = None
    review_approved: bool | None = None
    decided_at: datetime | None = None

    @property
    def online_eligible(self) -> bool:
        """Runtime learning cannot activate or publish a Skill."""

        return False

    @property
    def handoff_ready(self) -> bool:
        """Human approval only opens a separate, manual draft creation flow."""

        return self.status == REVIEW_PENDING_STATUS and self.review_approved is True

    def with_audit_event(self, kind: str, **details: object) -> "RuntimeSkillCandidate":
        return replace(
            self,
            audit_events=(
                *self.audit_events,
                {
                    "kind": kind,
                    "at": utc_now().isoformat(),
                    **details,
                },
            ),
        )


@dataclass(frozen=True)
class ReplayCase:
    """A fixed, effect-free case for validating a candidate procedure."""

    case_id: str
    expected_root_cause: str
    required_evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReplayResult:
    case_id: str
    passed: bool
    reason_code: str


@dataclass(frozen=True)
class ReplayReport:
    report_id: str
    candidate_id: str
    passed: bool
    pass_rate: float
    results: tuple[ReplayResult, ...]
    execution_mode: str = "replay_no_effect"


@dataclass(frozen=True)
class ShadowCase:
    """A production-shaped observation whose effect count must remain zero."""

    case_id: str
    observed_root_cause: str
    observed_evidence_ids: tuple[str, ...]
    effect_count: int = 0


@dataclass(frozen=True)
class ShadowResult:
    case_id: str
    passed: bool
    reason_code: str


@dataclass(frozen=True)
class ShadowReport:
    report_id: str
    candidate_id: str
    passed: bool
    pass_rate: float
    results: tuple[ShadowResult, ...]
    execution_mode: str = "shadow_no_effect"


@dataclass(frozen=True)
class ReviewGate:
    """A human decision. Approval permits a bridge, never activation."""

    reviewer: str
    approved: bool
    note: str


@dataclass(frozen=True)
class SkillCandidateHandoff:
    """Review-approved metadata for a separate human-controlled Skill flow."""

    candidate_id: str
    manifest: dict[str, object]
    procedure: dict[str, object]
    audit: dict[str, object]
    activation_allowed: bool = False
    requires_manual_draft_creation: bool = True


@dataclass(frozen=True)
class ObservationResult:
    """The observer reports why a task was not eligible without creating a Skill."""

    candidate: RuntimeSkillCandidate | None
    blocked_reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "CANDIDATE_STATUS",
    "REJECTED_STATUS",
    "REPLAY_PENDING_STATUS",
    "REVIEW_PENDING_STATUS",
    "SHADOW_STATUS",
    "ObservationResult",
    "OperatorFeedback",
    "ReplayCase",
    "ReplayReport",
    "ReplayResult",
    "ReviewGate",
    "RuntimeSkillCandidate",
    "RuntimeSkillLearningError",
    "ShadowCase",
    "ShadowReport",
    "ShadowResult",
    "SkillCandidateHandoff",
]
