"""Database records owned by the durable Agent Runtime execution plane."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from athena.api.repositories.models import Base


class RuntimeAgentTaskModel(Base):
    __tablename__ = "agent_tasks"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    goal: Mapped[str] = mapped_column(Text)
    repository_root: Mapped[str] = mapped_column(String(2048))
    profile: Mapped[str] = mapped_column(String(24), index=True)
    budget_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), index=True)
    final_report_json: Mapped[dict[str, object] | None] = mapped_column(
        JSON, nullable=True
    )
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    lease_generation: Mapped[int] = mapped_column(Integer, default=0)
    checkpoint_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now()
    )


class RuntimeCheckpointModel(Base):
    __tablename__ = "runtime_checkpoints"
    __table_args__ = (
        UniqueConstraint("task_id", "checkpoint_version", name="uq_runtime_checkpoint"),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(96), index=True)
    checkpoint_version: Mapped[int] = mapped_column(Integer)
    working_state_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    context_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class RuntimeTickEventModel(Base):
    __tablename__ = "runtime_tick_events"
    __table_args__ = (
        UniqueConstraint("task_id", "sequence", name="uq_runtime_tick_event_sequence"),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(96), index=True)
    tick_id: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(120), index=True)
    payload_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    tick_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    tick_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class RuntimeArtifactModel(Base):
    __tablename__ = "runtime_artifacts"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(96), index=True)
    tick_id: Mapped[str] = mapped_column(String(96), index=True)
    tool_name: Mapped[str] = mapped_column(String(160))
    content_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class RuntimeEvidenceModel(Base):
    __tablename__ = "runtime_evidence"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(96), index=True)
    artifact_id: Mapped[str] = mapped_column(String(96), index=True)
    source: Mapped[str] = mapped_column(String(160))
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class RuntimeUsageModel(Base):
    __tablename__ = "runtime_usage"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(96), index=True)
    tick_id: Mapped[str] = mapped_column(String(96), index=True)
    purpose: Mapped[str] = mapped_column(String(80))
    model_tier: Mapped[str] = mapped_column(String(80))
    route_reason: Mapped[str] = mapped_column(String(160))
    estimated_input_tokens: Mapped[int] = mapped_column(Integer)
    reserved_tokens: Mapped[int] = mapped_column(Integer)
    actual_input_tokens: Mapped[int] = mapped_column(Integer)
    actual_output_tokens: Mapped[int] = mapped_column(Integer)
    budget_mode: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )


class RuntimeToolEffectModel(Base):
    """Runtime-local idempotency journal for tool effects."""

    __tablename__ = "runtime_tool_effects"
    __table_args__ = (
        UniqueConstraint("task_id", "effect_id", name="uq_runtime_tool_effect"),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(96), index=True)
    effect_id: Mapped[str] = mapped_column(String(160), index=True)
    tool_name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(24), index=True)
    artifact_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    evidence_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RuntimeSkillMemoryModel(Base):
    """Durable compact projection of an evaluated Skill."""

    __tablename__ = "runtime_skill_memory"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    title: Mapped[str] = mapped_column(String(240))
    procedure_summary: Mapped[str] = mapped_column(Text)
    evaluation_state: Mapped[str] = mapped_column(String(32), index=True)
    source_references_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), index=True
    )


class RuntimeEpisodicMemoryModel(Base):
    """Tenant-scoped redacted history projected from eligible trajectories."""

    __tablename__ = "runtime_episodic_memory"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_task_id", name="uq_runtime_episodic_source_task"
        ),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    source_task_id: Mapped[str] = mapped_column(String(96), index=True)
    task_summary: Mapped[str] = mapped_column(Text)
    outcome_summary: Mapped[str] = mapped_column(Text)
    tool_names_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence_summaries_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    quality_score: Mapped[float] = mapped_column(Float, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class RuntimeSemanticMemoryModel(Base):
    """Curated tenant-scoped fact with an explicit approval lifecycle."""

    __tablename__ = "runtime_semantic_memory"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(120), index=True)
    domain: Mapped[str] = mapped_column(String(160), index=True)
    fact: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, index=True)
    source_trajectory_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(24), index=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), index=True
    )
