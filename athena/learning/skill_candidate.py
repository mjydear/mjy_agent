"""Governed, offline-only Skill Candidate domain and persistence model."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from athena.api.repositories.models import Base
from athena.learning.skill_validation import SKILL_CANDIDATE_SCHEMA_VERSION

CANDIDATE_STATUS = "candidate"
REPLAY_PENDING_STATUS = "replay_pending"
SHADOW_STATUS = "shadow"
REVIEW_PENDING_STATUS = "review_pending"
REJECTED_STATUS = "rejected"


class SkillCandidateError(RuntimeError):
    """Base error with a stable code safe to expose at an API boundary."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class SkillCandidateSourceError(SkillCandidateError):
    """Raised when learning provenance is missing, unverified, or unsafe."""


class SkillCandidateLifecycleError(SkillCandidateError):
    """Raised when a candidate lifecycle transition is not allowed."""


@dataclass(frozen=True)
class SkillCandidateProposal:
    """A request containing IDs only; source content is resolved by a trusted port."""

    tenant_id: str
    name: str
    workflow_type: str
    environment_type: str
    capabilities: tuple[str, ...]
    outcome_id: str
    feedback_id: str
    evidence_ids: tuple[str, ...]
    created_by: str


@dataclass(frozen=True)
class TrajectorySkillCandidateProposal:
    """Human-authored structured Candidate sourced only from eligible trajectories."""

    tenant_id: str
    name: str
    description: str
    trigger: dict[str, object]
    allowed_tools: tuple[str, ...]
    procedure: tuple[str, ...]
    failure_recovery: tuple[str, ...]
    success_contract: dict[str, object]
    evidence_requirements: tuple[str, ...]
    token_budget_hint: int
    source_trajectory_ids: tuple[str, ...]
    created_by: str
    version: int = 1
    risk_level: str = "S1"
    skill_id: str | None = None


@dataclass(frozen=True)
class VerifiedEvidenceSummary:
    """A bounded, already-approved Evidence summary, never raw Evidence content."""

    evidence_id: str
    summary: str


@dataclass(frozen=True)
class VerifiedLearningSource:
    """Trusted output of the Outcome/Feedback/Evidence adapter.

    The adapter is responsible for checking tenant ownership and verification state.
    The candidate service repeats the shape and safety checks before persistence.
    """

    tenant_id: str
    outcome_id: str
    feedback_id: str
    outcome_verified: bool
    feedback_verified: bool
    outcome_summary: str
    feedback_summary: str
    evidence: tuple[VerifiedEvidenceSummary, ...]

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.evidence)


class VerifiedLearningSourceResolver(Protocol):
    """Port implemented by the application layer over verified durable facts."""

    async def resolve(
        self,
        tenant_id: str,
        *,
        outcome_id: str,
        feedback_id: str,
        evidence_ids: tuple[str, ...],
    ) -> VerifiedLearningSource | None:
        """Return safe source material only when all references are tenant-valid."""


@dataclass(frozen=True)
class SkillCandidate:
    """Immutable snapshot of a candidate; it has no Active state or execution hook."""

    candidate_id: str
    tenant_id: str
    name: str
    workflow_type: str
    environment_type: str
    capabilities: tuple[str, ...]
    manifest: dict[str, object]
    procedure: dict[str, object]
    status: str
    source_outcome_id: str
    source_feedback_id: str
    evidence_ids: tuple[str, ...]
    source_digest: str
    source_summary: dict[str, object]
    created_by: str
    schema_version: str = SKILL_CANDIDATE_SCHEMA_VERSION
    skill_id: str = ""
    version: int = 1
    description: str = ""
    trigger: dict[str, object] | None = None
    allowed_tools: tuple[str, ...] = ()
    failure_recovery: tuple[str, ...] = ()
    success_contract: dict[str, object] | None = None
    evidence_requirements: tuple[str, ...] = ()
    token_budget_hint: int = 0
    source_trajectory_ids: tuple[str, ...] = ()
    evaluation_status: str = "not_evaluated"
    risk_level: str = "S1"
    audit_events: tuple[dict[str, object], ...] = field(default=(), compare=False)
    replay_report_id: str | None = None
    shadow_report_id: str | None = None
    reviewed_by: str | None = None
    review_note: str | None = None
    decided_at: datetime | None = None

    @property
    def online_eligible(self) -> bool:
        """Candidates are never eligible for online publication by this module."""

        return False


@dataclass(frozen=True)
class SkillCandidateBridge:
    """Auditable hand-off payload for a human-created SkillRepository Draft."""

    candidate_id: str
    tenant_id: str
    name: str
    environment_type: str
    capabilities: tuple[str, ...]
    manifest: dict[str, object]
    procedure: dict[str, object]
    source_outcome_id: str
    source_feedback_id: str
    evidence_ids: tuple[str, ...]
    replay_report_id: str | None
    shadow_report_id: str | None
    audit: dict[str, object]
    activation_allowed: bool = False


class SkillCandidateModel(Base):
    """Durable candidate facts; deliberately separate from SkillVersionModel."""

    __tablename__ = "skill_candidates"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_digest", name="uq_skill_candidate_source"
        ),
        CheckConstraint(
            "status IN ('candidate', 'replay_pending', 'shadow', "
            "'review_pending', 'rejected')",
            name="ck_skill_candidate_not_active",
        ),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(160))
    workflow_type: Mapped[str] = mapped_column(String(80), index=True)
    environment_type: Mapped[str] = mapped_column(String(80))
    capabilities_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    manifest_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    procedure_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), index=True)
    source_outcome_id: Mapped[str] = mapped_column(String(120), index=True)
    source_feedback_id: Mapped[str] = mapped_column(String(120), index=True)
    evidence_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_digest: Mapped[str] = mapped_column(String(128), index=True)
    source_summary_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(160))
    schema_version: Mapped[str] = mapped_column(
        String(64), default=SKILL_CANDIDATE_SCHEMA_VERSION
    )
    skill_id: Mapped[str] = mapped_column(String(96), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(Text, default="")
    trigger_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    allowed_tools_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    failure_recovery_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    success_contract_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    evidence_requirements_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    token_budget_hint: Mapped[int] = mapped_column(Integer, default=0)
    source_trajectory_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    evaluation_status: Mapped[str] = mapped_column(
        String(32), default="not_evaluated", index=True
    )
    risk_level: Mapped[str] = mapped_column(String(16), default="S1")
    audit_events_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    replay_report_id: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    shadow_report_id: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


_UNSAFE_SOURCE_PATTERN = re.compile(
    r"(?:hidden[\s_-]*thought|chain[\s_-]*of[\s_-]*thought|raw[\s_-]*prompt|"
    r"system[\s_-]*prompt|<\s*/?\s*think\s*>|"
    r"(?:api[\s_-]*key|access[\s_-]*token|password|authorization|bearer|secret)\s*[:=])",
    re.IGNORECASE,
)


def normalize_safe_summary(value: str, *, field: str) -> str:
    """Normalize bounded source text and reject common prompt/secret carriers."""

    if not isinstance(value, str) or not value.strip():
        raise SkillCandidateSourceError("SKILL_CANDIDATE_SOURCE_SUMMARY_REQUIRED")
    normalized = " ".join(value.split())
    if len(normalized) > 2000:
        raise SkillCandidateSourceError("SKILL_CANDIDATE_SOURCE_SUMMARY_TOO_LARGE")
    if _UNSAFE_SOURCE_PATTERN.search(normalized):
        raise SkillCandidateSourceError("SKILL_CANDIDATE_UNSAFE_SOURCE")
    return normalized


def source_digest(
    tenant_id: str,
    outcome_id: str,
    feedback_id: str,
    evidence_ids: tuple[str, ...],
) -> str:
    """Build a stable tenant-scoped deduplication key from verified references."""

    encoded = json.dumps(
        {
            "tenant_id": tenant_id,
            "outcome_id": outcome_id,
            "feedback_id": feedback_id,
            "evidence_ids": list(evidence_ids),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def trajectory_source_digest(
    tenant_id: str, source_trajectory_ids: tuple[str, ...]
) -> str:
    """Build a stable deduplication key for an eligible trajectory set."""

    encoded = json.dumps(
        {
            "tenant_id": tenant_id,
            "source_trajectory_ids": sorted(source_trajectory_ids),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def utc_or_none(value: datetime | None) -> datetime | None:
    """Return database timestamps in a consistent UTC representation."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "CANDIDATE_STATUS",
    "REJECTED_STATUS",
    "REPLAY_PENDING_STATUS",
    "REVIEW_PENDING_STATUS",
    "SHADOW_STATUS",
    "SkillCandidate",
    "SkillCandidateBridge",
    "SkillCandidateError",
    "SkillCandidateLifecycleError",
    "SkillCandidateModel",
    "SkillCandidateProposal",
    "SkillCandidateSourceError",
    "TrajectorySkillCandidateProposal",
    "VerifiedEvidenceSummary",
    "VerifiedLearningSource",
    "VerifiedLearningSourceResolver",
    "normalize_safe_summary",
    "source_digest",
    "trajectory_source_digest",
    "utc_or_none",
]
