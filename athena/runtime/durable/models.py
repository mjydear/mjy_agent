"""Database records owned by the durable Agent Runtime execution plane."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text, UniqueConstraint, func
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
    final_report_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    lease_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    lease_generation: Mapped[int] = mapped_column(Integer, default=0)
    checkpoint_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())


class RuntimeArtifactModel(Base):
    __tablename__ = "runtime_artifacts"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(96), index=True)
    tick_id: Mapped[str] = mapped_column(String(96), index=True)
    tool_name: Mapped[str] = mapped_column(String(160))
    content_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())


class RuntimeEvidenceModel(Base):
    __tablename__ = "runtime_evidence"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(96), index=True)
    artifact_id: Mapped[str] = mapped_column(String(96), index=True)
    source: Mapped[str] = mapped_column(String(160))
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())


class RuntimeToolEffectModel(Base):
    """Runtime-local idempotency journal; separate from CloudOps tool effects."""

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
