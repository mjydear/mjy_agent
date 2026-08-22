"""SQLAlchemy models for the Agent Runtime control plane.

The schema is intentionally limited to Runtime execution and governed Skill
learning. Domain adapters own their business data outside this control plane.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class OutboxMessageModel(Base):
    """Durable queue record used to hand off captured Shadow observations."""

    __tablename__ = "outbox_messages"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    aggregate_id: Mapped[str] = mapped_column(String(96), index=True)
    event_type: Mapped[str] = mapped_column(String(120), index=True)
    payload_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    traceparent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    lock_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class SkillDefinitionModel(Base):
    __tablename__ = "skill_definitions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_skill_definition_name"),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(160))
    owner: Mapped[str] = mapped_column(String(160))
    environment_type: Mapped[str] = mapped_column(String(80), default="backend")
    capabilities_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    active_version_id: Mapped[str | None] = mapped_column(
        String(96), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


class SkillVersionModel(Base):
    __tablename__ = "skill_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "skill_id", "version", name="uq_skill_version"),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    skill_id: Mapped[str] = mapped_column(String(96), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), index=True)
    manifest_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    procedure_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    checksum: Mapped[str] = mapped_column(String(128), index=True)
    source_task_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    benchmark_report_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by: Mapped[str] = mapped_column(String(160))
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


class LearningTrajectoryModel(Base):
    """Redacted Runtime trajectory fact; raw prompts and artifacts are excluded."""

    __tablename__ = "learning_trajectories"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_task_id", name="uq_learning_trajectory_task"
        ),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    source_task_id: Mapped[str] = mapped_column(String(96), index=True)
    schema_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), index=True)
    task_summary: Mapped[str] = mapped_column(Text)
    outcome_summary_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    tool_calls_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    evidence_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    usage_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    budget_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    admission_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    quality_score: Mapped[float] = mapped_column(Float)
    rejection_reasons_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    redaction_count: Mapped[int] = mapped_column(Integer, default=0)
    contains_raw_artifacts: Mapped[bool] = mapped_column(Boolean, default=False)
    contains_hidden_reasoning: Mapped[bool] = mapped_column(Boolean, default=False)
    admitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


class LearningTrajectoryEventModel(Base):
    """Append-only admission state transition."""

    __tablename__ = "learning_trajectory_events"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    trajectory_id: Mapped[str] = mapped_column(String(96), index=True)
    kind: Mapped[str] = mapped_column(String(80), index=True)
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str] = mapped_column(String(24))
    details_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class SkillCandidateGenerationRunModel(Base):
    """Audit record for generating one candidate from redacted trajectories."""

    __tablename__ = "skill_candidate_generation_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_digest",
            name="uq_skill_candidate_generation_source",
        ),
        CheckConstraint(
            "status IN ('started', 'succeeded', 'failed', 'duplicate', 'rejected')",
            name="ck_skill_candidate_generation_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    source_digest: Mapped[str] = mapped_column(String(128), index=True)
    source_trajectory_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), index=True)
    digest_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    generator: Mapped[str] = mapped_column(String(96))
    candidate_id: Mapped[str | None] = mapped_column(
        String(96), nullable=True, index=True
    )
    validation_report_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    duplicate_of_candidate_id: Mapped[str | None] = mapped_column(
        String(96), nullable=True
    )
    deduplication_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    usage_json: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SkillCandidateValidationReportModel(Base):
    """Immutable deterministic validation result for one candidate digest."""

    __tablename__ = "skill_candidate_validation_reports"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "candidate_digest",
            "validator_version",
            name="uq_skill_candidate_validation_digest",
        ),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    candidate_id: Mapped[str] = mapped_column(String(96), index=True)
    candidate_digest: Mapped[str] = mapped_column(String(128))
    validator_version: Mapped[str] = mapped_column(String(64))
    schema_valid: Mapped[bool] = mapped_column(Boolean)
    security_valid: Mapped[bool] = mapped_column(Boolean)
    passed: Mapped[bool] = mapped_column(Boolean, index=True)
    checks_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    violations_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class SkillBaselineRunModel(Base):
    """Fixed replay results produced without loading a candidate."""

    __tablename__ = "skill_baseline_runs"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    schema_version: Mapped[str] = mapped_column(String(64))
    case_definition_digest: Mapped[str] = mapped_column(String(128), index=True)
    runner: Mapped[str] = mapped_column(String(96))
    candidate_loaded: Mapped[bool] = mapped_column(Boolean, default=False)
    case_count: Mapped[int] = mapped_column(Integer)
    oracle_pass_count: Mapped[int] = mapped_column(Integer)
    results_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class SkillReplayABRunModel(Base):
    """Candidate-vs-baseline replay and publication gate result."""

    __tablename__ = "skill_replay_ab_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "candidate_digest",
            "case_definition_digest",
            "runner",
            name="uq_skill_replay_ab_identity",
        ),
        CheckConstraint(
            "status IN ('passed', 'rejected', 'evaluation_failed')",
            name="ck_skill_replay_ab_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    candidate_id: Mapped[str] = mapped_column(String(96), index=True)
    candidate_digest: Mapped[str] = mapped_column(String(128), index=True)
    validation_report_id: Mapped[str] = mapped_column(String(96))
    schema_version: Mapped[str] = mapped_column(String(64))
    case_definition_digest: Mapped[str] = mapped_column(String(128), index=True)
    runner: Mapped[str] = mapped_column(String(96))
    status: Mapped[str] = mapped_column(String(32), index=True)
    case_count: Mapped[int] = mapped_column(Integer)
    comparisons_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, default=list
    )
    aggregate_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    gate_checks_json: Mapped[dict[str, bool]] = mapped_column(JSON, default=dict)
    gate_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_reason: Mapped[str | None] = mapped_column(String(160), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class SkillShadowRunModel(Base):
    """Isolated candidate observations; this record never activates a Skill."""

    __tablename__ = "skill_shadow_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "candidate_digest",
            "case_definition_digest",
            "runner",
            name="uq_skill_shadow_identity",
        ),
        CheckConstraint(
            "status IN ('passed', 'rejected', 'evaluation_failed')",
            name="ck_skill_shadow_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    candidate_id: Mapped[str] = mapped_column(String(96), index=True)
    candidate_digest: Mapped[str] = mapped_column(String(128), index=True)
    validation_report_id: Mapped[str] = mapped_column(String(96), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    case_definition_digest: Mapped[str] = mapped_column(String(128), index=True)
    runner: Mapped[str] = mapped_column(String(96), nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True)
    case_count: Mapped[int] = mapped_column(Integer)
    comparisons_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, default=list
    )
    aggregate_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    gate_checks_json: Mapped[dict[str, bool]] = mapped_column(JSON, default=dict)
    gate_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_reason: Mapped[str | None] = mapped_column(String(160), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class ShadowTrafficObservationModel(Base):
    """One redacted production-shaped trace queued for Shadow replay."""

    __tablename__ = "shadow_traffic_observations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "trace_id",
            "candidate_id",
            "candidate_digest",
            name="uq_shadow_traffic_identity",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_shadow_traffic_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    trace_id: Mapped[str] = mapped_column(String(128), index=True)
    candidate_id: Mapped[str] = mapped_column(String(96), index=True)
    candidate_digest: Mapped[str] = mapped_column(String(128), index=True)
    envelope_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    baseline_metrics_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    candidate_metrics_json: Mapped[dict[str, object]] = mapped_column(
        JSON, default=dict
    )
    comparison_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    failure_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
