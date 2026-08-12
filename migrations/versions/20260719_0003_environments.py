"""Create tenant-scoped CloudOps environment declarations."""

import sqlalchemy as sa
from alembic import op

revision = "20260719_0003"
down_revision = "20260718_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "environments",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("environment_type", sa.String(40), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("credential_ref", sa.String(256)),
        sa.Column("capabilities_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_environment_name"),
    )
    op.create_index("ix_environments_tenant_id", "environments", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("environments")
