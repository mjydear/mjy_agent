"""Add auditable Candidate generation runs.

Revision ID: 20260812_0012
Revises: 20260812_0011
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260812_0012"
down_revision = "20260812_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_candidate_generation_runs",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("source_digest", sa.String(128), nullable=False),
        sa.Column("source_trajectory_ids_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("digest_json", sa.JSON(), nullable=False),
        sa.Column("generator", sa.String(96), nullable=False),
        sa.Column("candidate_id", sa.String(96), nullable=True),
        sa.Column("validation_report_id", sa.String(96), nullable=True),
        sa.Column("duplicate_of_candidate_id", sa.String(96), nullable=True),
        sa.Column("deduplication_json", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(160), nullable=True),
        sa.Column("usage_json", sa.JSON(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("failure_code", sa.String(120), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "source_digest",
            name="uq_skill_candidate_generation_source",
        ),
        sa.CheckConstraint(
            "status IN ('started', 'succeeded', 'failed', 'duplicate', 'rejected')",
            name="ck_skill_candidate_generation_status",
        ),
    )
    for column in ("tenant_id", "source_digest", "status", "candidate_id"):
        op.create_index(
            f"ix_skill_candidate_generation_runs_{column}",
            "skill_candidate_generation_runs",
            [column],
        )


def downgrade() -> None:
    for column in ("candidate_id", "status", "source_digest", "tenant_id"):
        op.drop_index(
            f"ix_skill_candidate_generation_runs_{column}",
            table_name="skill_candidate_generation_runs",
        )
    op.drop_table("skill_candidate_generation_runs")
