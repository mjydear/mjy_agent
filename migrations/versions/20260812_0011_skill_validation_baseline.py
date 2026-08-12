"""Add Candidate validation reports and fixed Baseline observations.

Revision ID: 20260812_0011
Revises: 20260812_0010
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260812_0011"
down_revision = "20260812_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("skill_candidates") as batch_op:
        batch_op.add_column(
            sa.Column(
                "schema_version",
                sa.String(64),
                nullable=False,
                server_default="athena.skill-candidate.v1",
            )
        )

    op.create_table(
        "skill_candidate_validation_reports",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("candidate_id", sa.String(96), nullable=False),
        sa.Column("candidate_digest", sa.String(128), nullable=False),
        sa.Column("validator_version", sa.String(64), nullable=False),
        sa.Column("schema_valid", sa.Boolean(), nullable=False),
        sa.Column("security_valid", sa.Boolean(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("checks_json", sa.JSON(), nullable=False),
        sa.Column("violations_json", sa.JSON(), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_id",
            "candidate_digest",
            "validator_version",
            name="uq_skill_candidate_validation_digest",
        ),
    )
    for column in ("tenant_id", "candidate_id", "passed"):
        op.create_index(
            f"ix_skill_candidate_validation_reports_{column}",
            "skill_candidate_validation_reports",
            [column],
        )

    op.create_table(
        "skill_baseline_runs",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("case_definition_digest", sa.String(128), nullable=False),
        sa.Column("runner", sa.String(96), nullable=False),
        sa.Column("candidate_loaded", sa.Boolean(), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("oracle_pass_count", sa.Integer(), nullable=False),
        sa.Column("results_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("tenant_id", "case_definition_digest"):
        op.create_index(
            f"ix_skill_baseline_runs_{column}",
            "skill_baseline_runs",
            [column],
        )


def downgrade() -> None:
    for column in ("case_definition_digest", "tenant_id"):
        op.drop_index(
            f"ix_skill_baseline_runs_{column}", table_name="skill_baseline_runs"
        )
    op.drop_table("skill_baseline_runs")

    for column in ("passed", "candidate_id", "tenant_id"):
        op.drop_index(
            f"ix_skill_candidate_validation_reports_{column}",
            table_name="skill_candidate_validation_reports",
        )
    op.drop_table("skill_candidate_validation_reports")

    with op.batch_alter_table("skill_candidates") as batch_op:
        batch_op.drop_column("schema_version")
