"""Create durable task, event, outbox, alert and evidence facts.

Revision ID: 20260718_0001
Revises:
Create Date: 2026-07-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260718_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ops_tasks",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("workflow_type", sa.String(80), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("environment_id", sa.String(120), nullable=False),
        sa.Column("environment_mode", sa.String(20), nullable=False),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("policy_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("config_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("budget_json", sa.JSON(), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("execution_profile", sa.String(80), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("phase", sa.String(24), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(160)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("checkpoint_version", sa.Integer(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("trigger_type", sa.String(80), nullable=False),
        sa.Column("trigger_ref", sa.String(200)),
        sa.Column("traceparent", sa.String(256)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in (
        "tenant_id",
        "environment_id",
        "status",
        "phase",
        "lease_expires_at",
        "next_run_at",
    ):
        op.create_index(f"ix_ops_tasks_{column}", "ops_tasks", [column])

    op.create_table(
        "task_execution_snapshots",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "task_id", name="uq_snapshot_task"),
    )
    op.create_index(
        "ix_task_execution_snapshots_tenant_id",
        "task_execution_snapshots",
        ["tenant_id"],
    )
    op.create_index(
        "ix_task_execution_snapshots_task_id", "task_execution_snapshots", ["task_id"]
    )

    op.create_table(
        "task_events",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(120), nullable=False),
        sa.Column("data_json", sa.JSON(), nullable=False),
        sa.Column("trace_id", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "task_id", "sequence", name="uq_task_event_sequence"
        ),
    )
    for column in ("tenant_id", "task_id", "created_at"):
        op.create_index(f"ix_task_events_{column}", "task_events", [column])

    op.create_table(
        "task_checkpoints",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("checkpoint_version", sa.Integer(), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "task_id", "checkpoint_version", name="uq_task_checkpoint"
        ),
    )
    op.create_index("ix_task_checkpoints_tenant_id", "task_checkpoints", ["tenant_id"])
    op.create_index("ix_task_checkpoints_task_id", "task_checkpoints", ["task_id"])

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("operation", sa.String(120), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("request_hash", sa.String(128), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "operation", "idempotency_key", name="uq_idempotency"
        ),
    )
    op.create_index(
        "ix_idempotency_records_tenant_id", "idempotency_records", ["tenant_id"]
    )

    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("aggregate_id", sa.String(80), nullable=False),
        sa.Column("event_type", sa.String(120), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("traceparent", sa.String(256)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("lock_owner", sa.String(160)),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in (
        "tenant_id",
        "aggregate_id",
        "event_type",
        "available_at",
        "published_at",
        "locked_until",
    ):
        op.create_index(f"ix_outbox_messages_{column}", "outbox_messages", [column])

    op.create_table(
        "alert_receipts",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("integration_id", sa.String(120), nullable=False),
        sa.Column("payload_hash", sa.String(128), nullable=False),
        sa.Column("external_event_id", sa.String(256)),
        sa.Column("canonical_fingerprint", sa.String(128), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "integration_id", "payload_hash", name="uq_alert_receipt"
        ),
    )
    for column in ("tenant_id", "task_id", "canonical_fingerprint"):
        op.create_index(f"ix_alert_receipts_{column}", "alert_receipts", [column])

    op.create_table(
        "alert_instances",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("canonical_fingerprint", sa.String(128), nullable=False),
        sa.Column("fingerprint_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "canonical_fingerprint",
            "fingerprint_version",
            name="uq_alert_instance",
        ),
    )
    for column in ("tenant_id", "task_id", "canonical_fingerprint"):
        op.create_index(f"ix_alert_instances_{column}", "alert_instances", [column])

    op.create_table(
        "evidences",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("evidence_type", sa.String(80), nullable=False),
        sa.Column("source", sa.String(160), nullable=False),
        sa.Column("data_origin", sa.String(24), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("content_ref", sa.String(512), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "task_id", "id", name="uq_evidence_task"),
    )
    op.create_index("ix_evidences_tenant_id", "evidences", ["tenant_id"])
    op.create_index("ix_evidences_task_id", "evidences", ["task_id"])


def downgrade() -> None:
    op.drop_table("evidences")
    op.drop_table("alert_instances")
    op.drop_table("alert_receipts")
    op.drop_table("outbox_messages")
    op.drop_table("idempotency_records")
    op.drop_table("task_checkpoints")
    op.drop_table("task_events")
    op.drop_table("task_execution_snapshots")
    op.drop_table("ops_tasks")
