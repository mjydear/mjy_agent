"""Create diagnosis outcome, operator feedback, recovery and skill candidate tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260809_0007"
down_revision = "20260719_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "diagnosis_outcomes",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("supporting_evidence_ids_json", sa.JSON(), nullable=False),
        sa.Column("remediation_recommendation", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_sufficient", sa.Boolean(), nullable=False),
        sa.Column("outcome_hash", sa.String(128), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "task_id", name="uq_diagnosis_outcome_task"),
    )
    for column in ("tenant_id", "task_id", "outcome_hash", "finalized_at"):
        op.create_index(
            f"ix_diagnosis_outcomes_{column}", "diagnosis_outcomes", [column]
        )

    op.create_table(
        "operator_feedback",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("outcome_id", sa.String(80), nullable=False),
        sa.Column("feedback_type", sa.String(24), nullable=False),
        sa.Column("corrected_root_cause", sa.Text(), nullable=True),
        sa.Column(
            "corrected_remediation_recommendation", sa.Text(), nullable=True
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("submitted_by", sa.String(160), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("request_hash", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_operator_feedback_idempotency"
        ),
    )
    for column in (
        "tenant_id",
        "task_id",
        "outcome_id",
        "feedback_type",
        "request_hash",
        "created_at",
    ):
        op.create_index(f"ix_operator_feedback_{column}", "operator_feedback", [column])

    op.create_table(
        "diagnosis_recoveries",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("outcome_id", sa.String(80), nullable=False),
        sa.Column("feedback_id", sa.String(80), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "feedback_id", name="uq_recovery_feedback"),
    )
    for column in (
        "tenant_id",
        "task_id",
        "outcome_id",
        "feedback_id",
        "observed_at",
    ):
        op.create_index(
            f"ix_diagnosis_recoveries_{column}", "diagnosis_recoveries", [column]
        )

    op.create_table(
        "skill_candidates",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("workflow_type", sa.String(80), nullable=False),
        sa.Column("environment_type", sa.String(80), nullable=False),
        sa.Column("capabilities_json", sa.JSON(), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("procedure_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_outcome_id", sa.String(120), nullable=False),
        sa.Column("source_feedback_id", sa.String(120), nullable=False),
        sa.Column("evidence_ids_json", sa.JSON(), nullable=False),
        sa.Column("source_digest", sa.String(128), nullable=False),
        sa.Column("source_summary_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(160), nullable=False),
        sa.Column("replay_report_id", sa.String(160), nullable=True),
        sa.Column("shadow_report_id", sa.String(160), nullable=True),
        sa.Column("reviewed_by", sa.String(160), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "source_digest", name="uq_skill_candidate_source"
        ),
    )
    for column in (
        "tenant_id",
        "workflow_type",
        "status",
        "source_outcome_id",
        "source_feedback_id",
        "source_digest",
    ):
        op.create_index(f"ix_skill_candidates_{column}", "skill_candidates", [column])


def downgrade() -> None:
    op.drop_table("skill_candidates")
    op.drop_table("diagnosis_recoveries")
    op.drop_table("operator_feedback")
    op.drop_table("diagnosis_outcomes")
