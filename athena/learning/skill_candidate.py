"""Governed, offline-only Skill Candidate domain and persistence model."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import JSON, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from athena.api.repositories.models import Base

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
    "VerifiedEvidenceSummary",
    "VerifiedLearningSource",
    "VerifiedLearningSourceResolver",
    "normalize_safe_summary",
    "source_digest",
    "utc_or_none",
]
