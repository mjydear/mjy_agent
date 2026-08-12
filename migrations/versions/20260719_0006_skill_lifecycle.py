"""Create governed skill definition and version tables."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260719_0006"
down_revision = "20260719_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_definitions",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("owner", sa.String(160), nullable=False),
        sa.Column(
            "environment_type",
            sa.String(80),
            nullable=False,
            server_default="kubernetes",
        ),
        sa.Column("capabilities_json", sa.JSON(), nullable=False),
        sa.Column("active_version_id", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_skill_definition_name"),
    )
    op.create_index(
        "ix_skill_definitions_tenant_id", "skill_definitions", ["tenant_id"]
    )
    op.create_index(
        "ix_skill_definitions_active_version_id",
        "skill_definitions",
        ["active_version_id"],
    )

    op.create_table(
        "skill_versions",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("skill_id", sa.String(80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("procedure_json", sa.JSON(), nullable=False),
        sa.Column("checksum", sa.String(128), nullable=False),
        sa.Column("source_task_id", sa.String(80)),
        sa.Column("benchmark_report_id", sa.String(120)),
        sa.Column("created_by", sa.String(160), nullable=False),
        sa.Column("reviewed_by", sa.String(160)),
        sa.Column("review_note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "tenant_id", "skill_id", "version", name="uq_skill_version"
        ),
    )
    op.create_index("ix_skill_versions_tenant_id", "skill_versions", ["tenant_id"])
    op.create_index("ix_skill_versions_skill_id", "skill_versions", ["skill_id"])
    op.create_index("ix_skill_versions_status", "skill_versions", ["status"])
    op.create_index("ix_skill_versions_checksum", "skill_versions", ["checksum"])


def downgrade() -> None:
    op.drop_table("skill_versions")
    op.drop_table("skill_definitions")
