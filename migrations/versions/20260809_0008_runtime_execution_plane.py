"""Create durable records for the generic Agent Runtime execution plane.

Revision ID: 20260809_0008
Revises: 20260809_0007
Create Date: 2026-08-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260809_0008"
down_revision = "20260809_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_tasks",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("repository_root", sa.String(2048), nullable=False),
        sa.Column("profile", sa.String(24), nullable=False),
        sa.Column("budget_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("final_report_json", sa.JSON(), nullable=True),
        sa.Column("cancellation_requested", sa.Boolean(), nullable=False),
        sa.Column("lease_id", sa.String(160), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("checkpoint_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("profile", "status", "lease_expires_at"):
        op.create_index(f"ix_agent_tasks_{column}", "agent_tasks", [column])

    op.create_table(
        "runtime_checkpoints",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("task_id", sa.String(96), nullable=False),
        sa.Column("checkpoint_version", sa.Integer(), nullable=False),
        sa.Column("working_state_json", sa.JSON(), nullable=False),
        sa.Column("context_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "checkpoint_version", name="uq_runtime_checkpoint"),
    )
    op.create_index("ix_runtime_checkpoints_task_id", "runtime_checkpoints", ["task_id"])

    op.create_table(
        "runtime_tick_events",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("task_id", sa.String(96), nullable=False),
        sa.Column("tick_id", sa.String(96), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(120), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("tick_sequence", sa.Integer(), nullable=True),
        sa.Column("decision_json", sa.JSON(), nullable=True),
        sa.Column("tick_status", sa.String(24), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "sequence", name="uq_runtime_tick_event_sequence"),
    )
    for column in ("task_id", "tick_id", "kind"):
        op.create_index(f"ix_runtime_tick_events_{column}", "runtime_tick_events", [column])

    op.create_table(
        "runtime_artifacts",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("task_id", sa.String(96), nullable=False),
        sa.Column("tick_id", sa.String(96), nullable=False),
        sa.Column("tool_name", sa.String(160), nullable=False),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("task_id", "tick_id", "content_hash"):
        op.create_index(f"ix_runtime_artifacts_{column}", "runtime_artifacts", [column])

    op.create_table(
        "runtime_evidence",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("task_id", sa.String(96), nullable=False),
        sa.Column("artifact_id", sa.String(96), nullable=False),
        sa.Column("source", sa.String(160), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("task_id", "artifact_id"):
        op.create_index(f"ix_runtime_evidence_{column}", "runtime_evidence", [column])

    op.create_table(
        "runtime_usage",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("task_id", sa.String(96), nullable=False),
        sa.Column("tick_id", sa.String(96), nullable=False),
        sa.Column("purpose", sa.String(80), nullable=False),
        sa.Column("model_tier", sa.String(80), nullable=False),
        sa.Column("route_reason", sa.String(160), nullable=False),
        sa.Column("estimated_input_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("actual_input_tokens", sa.Integer(), nullable=False),
        sa.Column("actual_output_tokens", sa.Integer(), nullable=False),
        sa.Column("budget_mode", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("task_id", "tick_id"):
        op.create_index(f"ix_runtime_usage_{column}", "runtime_usage", [column])


def downgrade() -> None:
    op.drop_table("runtime_usage")
    op.drop_table("runtime_evidence")
    op.drop_table("runtime_artifacts")
    op.drop_table("runtime_tick_events")
    op.drop_table("runtime_checkpoints")
    op.drop_table("agent_tasks")
