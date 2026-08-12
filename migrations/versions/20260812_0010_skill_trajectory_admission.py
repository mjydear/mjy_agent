"""Add redacted learning trajectories and complete Skill Candidate schema.

Revision ID: 20260812_0010
Revises: 20260809_0009
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260812_0010"
down_revision = "20260809_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_trajectories",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("source_task_id", sa.String(96), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("task_summary", sa.Text(), nullable=False),
        sa.Column("outcome_summary_json", sa.JSON(), nullable=False),
        sa.Column("tool_calls_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("usage_json", sa.JSON(), nullable=False),
        sa.Column("budget_json", sa.JSON(), nullable=False),
        sa.Column("admission_json", sa.JSON(), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=False),
        sa.Column("rejection_reasons_json", sa.JSON(), nullable=False),
        sa.Column("redaction_count", sa.Integer(), nullable=False),
        sa.Column("contains_raw_artifacts", sa.Boolean(), nullable=False),
        sa.Column("contains_hidden_reasoning", sa.Boolean(), nullable=False),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "source_task_id", name="uq_learning_trajectory_task"
        ),
    )
    for column in ("tenant_id", "source_task_id", "status"):
        op.create_index(
            f"ix_learning_trajectories_{column}",
            "learning_trajectories",
            [column],
        )

    op.create_table(
        "learning_trajectory_events",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("trajectory_id", sa.String(96), nullable=False),
        sa.Column("kind", sa.String(80), nullable=False),
        sa.Column("from_status", sa.String(24), nullable=True),
        sa.Column("to_status", sa.String(24), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("tenant_id", "trajectory_id", "kind"):
        op.create_index(
            f"ix_learning_trajectory_events_{column}",
            "learning_trajectory_events",
            [column],
        )

    with op.batch_alter_table("skill_candidates") as batch_op:
        batch_op.add_column(
            sa.Column("skill_id", sa.String(96), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("description", sa.Text(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("trigger_json", sa.JSON(), nullable=False, server_default="{}")
        )
        batch_op.add_column(
            sa.Column(
                "allowed_tools_json", sa.JSON(), nullable=False, server_default="[]"
            )
        )
        batch_op.add_column(
            sa.Column(
                "failure_recovery_json", sa.JSON(), nullable=False, server_default="[]"
            )
        )
        batch_op.add_column(
            sa.Column(
                "success_contract_json", sa.JSON(), nullable=False, server_default="{}"
            )
        )
        batch_op.add_column(
            sa.Column(
                "evidence_requirements_json",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )
        batch_op.add_column(
            sa.Column(
                "token_budget_hint", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch_op.add_column(
            sa.Column(
                "source_trajectory_ids_json",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )
        batch_op.add_column(
            sa.Column(
                "evaluation_status",
                sa.String(32),
                nullable=False,
                server_default="not_evaluated",
            )
        )
        batch_op.add_column(
            sa.Column("risk_level", sa.String(16), nullable=False, server_default="S1")
        )
        batch_op.add_column(
            sa.Column(
                "audit_events_json", sa.JSON(), nullable=False, server_default="[]"
            )
        )
        batch_op.create_check_constraint(
            "ck_skill_candidate_not_active",
            "status IN ('candidate', 'replay_pending', 'shadow', "
            "'review_pending', 'rejected')",
        )
    op.create_index("ix_skill_candidates_skill_id", "skill_candidates", ["skill_id"])
    op.create_index(
        "ix_skill_candidates_evaluation_status",
        "skill_candidates",
        ["evaluation_status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_skill_candidates_evaluation_status", table_name="skill_candidates"
    )
    op.drop_index("ix_skill_candidates_skill_id", table_name="skill_candidates")
    with op.batch_alter_table("skill_candidates") as batch_op:
        batch_op.drop_constraint("ck_skill_candidate_not_active", type_="check")
        for column in (
            "audit_events_json",
            "risk_level",
            "evaluation_status",
            "source_trajectory_ids_json",
            "token_budget_hint",
            "evidence_requirements_json",
            "success_contract_json",
            "failure_recovery_json",
            "allowed_tools_json",
            "trigger_json",
            "description",
            "version",
            "skill_id",
        ):
            batch_op.drop_column(column)
    op.drop_table("learning_trajectory_events")
    op.drop_table("learning_trajectories")
