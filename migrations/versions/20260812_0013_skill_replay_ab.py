"""Add reproducible Candidate-vs-Baseline Replay A/B reports.

Revision ID: 20260812_0013
Revises: 20260812_0012
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260812_0013"
down_revision = "20260812_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_replay_ab_runs",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("candidate_id", sa.String(96), nullable=False),
        sa.Column("candidate_digest", sa.String(128), nullable=False),
        sa.Column("validation_report_id", sa.String(96), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("case_definition_digest", sa.String(128), nullable=False),
        sa.Column("runner", sa.String(96), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("comparisons_json", sa.JSON(), nullable=False),
        sa.Column("aggregate_json", sa.JSON(), nullable=False),
        sa.Column("gate_checks_json", sa.JSON(), nullable=False),
        sa.Column("gate_passed", sa.Boolean(), nullable=False),
        sa.Column("failure_reason", sa.String(160), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "candidate_digest",
            "case_definition_digest",
            "runner",
            name="uq_skill_replay_ab_identity",
        ),
        sa.CheckConstraint(
            "status IN ('passed', 'rejected', 'evaluation_failed')",
            name="ck_skill_replay_ab_status",
        ),
    )
    for column in (
        "tenant_id",
        "candidate_id",
        "candidate_digest",
        "case_definition_digest",
        "status",
    ):
        op.create_index(
            f"ix_skill_replay_ab_runs_{column}",
            "skill_replay_ab_runs",
            [column],
        )


def downgrade() -> None:
    for column in (
        "status",
        "case_definition_digest",
        "candidate_digest",
        "candidate_id",
        "tenant_id",
    ):
        op.drop_index(
            f"ix_skill_replay_ab_runs_{column}",
            table_name="skill_replay_ab_runs",
        )
    op.drop_table("skill_replay_ab_runs")
