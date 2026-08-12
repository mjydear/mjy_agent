"""Create durable LLM config and encrypted secret tables."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260719_0005"
down_revision = "20260719_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "secret_records",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("credential_ref", sa.String(160), nullable=False),
        sa.Column(
            "key_version",
            sa.String(80),
            nullable=False,
            server_default="local-fernet-v1",
        ),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "credential_ref", name="uq_secret_record_ref"),
    )
    op.create_index("ix_secret_records_tenant_id", "secret_records", ["tenant_id"])
    op.create_index(
        "ix_secret_records_credential_ref", "secret_records", ["credential_ref"]
    )

    op.create_table(
        "llm_configs",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("config_id", sa.String(80), nullable=False),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("display_name", sa.String(160), nullable=False),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("credential_ref", sa.String(160)),
        sa.Column("credential_suffix", sa.String(16)),
        sa.Column("base_url", sa.String(512)),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "status", sa.String(24), nullable=False, server_default="available"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "config_id", name="uq_llm_config_ref"),
    )
    op.create_index("ix_llm_configs_tenant_id", "llm_configs", ["tenant_id"])
    op.create_index("ix_llm_configs_config_id", "llm_configs", ["config_id"])
    op.create_index("ix_llm_configs_status", "llm_configs", ["status"])


def downgrade() -> None:
    op.drop_table("llm_configs")
    op.drop_table("secret_records")
