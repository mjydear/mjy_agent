"""Create idempotent durable Tool effect records.

Revision ID: 20260718_0002
Revises: 20260718_0001
Create Date: 2026-07-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260718_0002"
down_revision = "20260718_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tool_effects",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("task_id", sa.String(80), nullable=False),
        sa.Column("call_id", sa.String(160), nullable=False),
        sa.Column("tool_name", sa.String(160), nullable=False),
        sa.Column("plan_hash", sa.String(128)),
        sa.Column("request_hash", sa.String(128), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("result_json", sa.JSON()),
        sa.Column("post_condition_json", sa.JSON()),
        sa.Column("error_code", sa.String(120)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "tenant_id", "task_id", "call_id", name="uq_tool_effect_call"
        ),
    )
    op.create_index("ix_tool_effects_tenant_id", "tool_effects", ["tenant_id"])
    op.create_index("ix_tool_effects_task_id", "tool_effects", ["task_id"])


def downgrade() -> None:
    op.drop_table("tool_effects")
