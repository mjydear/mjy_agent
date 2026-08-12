"""SQLAlchemy models for durable Agent task facts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
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


class EnvironmentModel(Base):
    __tablename__ = "environments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_environment_name"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(120))
    environment_type: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(80))
    mode: Mapped[str] = mapped_column(String(16))
    scope_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    credential_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    capabilities_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="unknown")
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


class SecretRecordModel(Base):
    __tablename__ = "secret_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "credential_ref", name="uq_secret_record_ref"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    credential_ref: Mapped[str] = mapped_column(String(160), index=True)
    key_version: Mapped[str] = mapped_column(String(80), default="local-fernet-v1")
    ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


class LLMConfigModel(Base):
    __tablename__ = "llm_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "config_id", name="uq_llm_config_ref"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    config_id: Mapped[str] = mapped_column(String(80), index=True)
    provider: Mapped[str] = mapped_column(String(80))
    display_name: Mapped[str] = mapped_column(String(160))
    model: Mapped[str] = mapped_column(String(160))
    credential_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)
    credential_suffix: Mapped[str | None] = mapped_column(String(16), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="available", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


class OpsTaskModel(Base):
    __tablename__ = "ops_tasks"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    workflow_type: Mapped[str] = mapped_column(String(80), default="crashloop")
    objective: Mapped[str] = mapped_column(Text)
    environment_id: Mapped[str] = mapped_column(String(120), index=True)
    environment_mode: Mapped[str] = mapped_column(String(20))
    scope_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    policy_snapshot_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    config_snapshot_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    budget_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    state_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    execution_profile: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), index=True)
    phase: Mapped[str] = mapped_column(String(24), index=True)
    state_version: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    lease_generation: Mapped[int] = mapped_column(Integer, default=0)
    checkpoint_version: Mapped[int] = mapped_column(Integer, default=0)
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    trigger_type: Mapped[str] = mapped_column(String(80), default="api")
    trigger_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    traceparent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


class TaskExecutionSnapshotModel(Base):
    __tablename__ = "task_execution_snapshots"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", name="uq_snapshot_task"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    snapshot_json: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class TaskEventModel(Base):
    __tablename__ = "task_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "task_id", "sequence", name="uq_task_event_sequence"
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(120))
    data_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), index=True
    )


class TaskCheckpointModel(Base):
    __tablename__ = "task_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "task_id", "checkpoint_version", name="uq_task_checkpoint"
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    checkpoint_version: Mapped[int] = mapped_column(Integer)
    state_version: Mapped[int] = mapped_column(Integer)
    lease_generation: Mapped[int] = mapped_column(Integer)
    state_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class IdempotencyRecordModel(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "operation", "idempotency_key", name="uq_idempotency"
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    operation: Mapped[str] = mapped_column(String(120))
    idempotency_key: Mapped[str] = mapped_column(String(256))
    request_hash: Mapped[str] = mapped_column(String(128))
    response_json: Mapped[dict[str, object]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class OutboxMessageModel(Base):
    __tablename__ = "outbox_messages"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    aggregate_id: Mapped[str] = mapped_column(String(80), index=True)
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


class AlertReceiptModel(Base):
    __tablename__ = "alert_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "integration_id", "payload_hash", name="uq_alert_receipt"
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    integration_id: Mapped[str] = mapped_column(String(120))
    payload_hash: Mapped[str] = mapped_column(String(128))
    external_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    canonical_fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    payload_json: Mapped[dict[str, object]] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class AlertInstanceModel(Base):
    __tablename__ = "alert_instances"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "canonical_fingerprint",
            "fingerprint_version",
            name="uq_alert_instance",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    canonical_fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    fingerprint_version: Mapped[str] = mapped_column(String(32), default="v1")
    status: Mapped[str] = mapped_column(String(24), default="active")
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


class EvidenceModel(Base):
    __tablename__ = "evidences"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", "id", name="uq_evidence_task"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    evidence_type: Mapped[str] = mapped_column(String(80))
    source: Mapped[str] = mapped_column(String(160))
    data_origin: Mapped[str] = mapped_column(String(24))
    summary: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(128))
    content_ref: Mapped[str] = mapped_column(String(512))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class DiagnosisOutcomeModel(Base):
    """Immutable, tenant-scoped result of one Diagnostic Task."""

    __tablename__ = "diagnosis_outcomes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", name="uq_diagnosis_outcome_task"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    supporting_evidence_ids_json: Mapped[list[str]] = mapped_column(
        JSON, default=list
    )
    remediation_recommendation: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    confidence: Mapped[float] = mapped_column(Float)
    evidence_sufficient: Mapped[bool] = mapped_column(Boolean)
    outcome_hash: Mapped[str] = mapped_column(String(128), index=True)
    finalized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class OperatorFeedbackModel(Base):
    """Immutable operator assessment linked to one Diagnosis Outcome."""

    __tablename__ = "operator_feedback"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_operator_feedback_idempotency"
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    outcome_id: Mapped[str] = mapped_column(String(80), index=True)
    feedback_type: Mapped[str] = mapped_column(String(24), index=True)
    corrected_root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_remediation_recommendation: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_by: Mapped[str] = mapped_column(String(160))
    idempotency_key: Mapped[str] = mapped_column(String(256))
    request_hash: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), index=True
    )


class RecoveryModel(Base):
    """Optional observed Recovery attached to one operator feedback record."""

    __tablename__ = "diagnosis_recoveries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "feedback_id", name="uq_recovery_feedback"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    outcome_id: Mapped[str] = mapped_column(String(80), index=True)
    feedback_id: Mapped[str] = mapped_column(String(80), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class ToolEffectModel(Base):
    __tablename__ = "tool_effects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", "call_id", name="uq_tool_effect_call"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    call_id: Mapped[str] = mapped_column(String(160))
    tool_name: Mapped[str] = mapped_column(String(160))
    plan_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24), default="started")
    result_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    post_condition_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON, nullable=True
    )
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OperationPlanModel(Base):
    __tablename__ = "operation_plans"
    __table_args__ = (
        UniqueConstraint("tenant_id", "plan_hash", name="uq_operation_plan_hash"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    task_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    environment_id: Mapped[str] = mapped_column(String(120), index=True)
    action_type: Mapped[str] = mapped_column(String(120))
    resource_kind: Mapped[str] = mapped_column(String(80))
    resource_name: Mapped[str] = mapped_column(String(160))
    namespace: Mapped[str] = mapped_column(String(120))
    risk_level: Mapped[str] = mapped_column(String(16))
    required_scope: Mapped[str] = mapped_column(String(120))
    plan_hash: Mapped[str] = mapped_column(String(128), index=True)
    canonical_json: Mapped[dict[str, object]] = mapped_column(JSON)
    parameters_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    preconditions_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    postconditions_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    rollback_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    dry_run_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    created_by: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class ApprovalModel(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    plan_id: Mapped[str] = mapped_column(String(80), index=True)
    plan_hash: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    requested_by: Mapped[str] = mapped_column(String(160))
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), index=True
    )
    decided_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    scopes_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class SkillDefinitionModel(Base):
    __tablename__ = "skill_definitions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_skill_definition_name"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(160))
    owner: Mapped[str] = mapped_column(String(160))
    environment_type: Mapped[str] = mapped_column(String(80), default="kubernetes")
    capabilities_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    active_version_id: Mapped[str | None] = mapped_column(
        String(80), nullable=True, index=True
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

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    skill_id: Mapped[str] = mapped_column(String(80), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), index=True)
    manifest_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    procedure_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    checksum: Mapped[str] = mapped_column(String(128), index=True)
    source_task_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
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
    """Redacted Runtime trajectory fact; never stores raw Artifacts or prompts."""

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
    """Append-only audit event for trajectory admission state changes."""

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


class SkillCandidateValidationReportModel(Base):
    """Immutable deterministic validation result for one Candidate digest."""

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
    """Observed fixed-case Baseline results; never stores Candidate output."""

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
