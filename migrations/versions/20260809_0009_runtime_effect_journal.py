"""Add idempotency records for the generic Runtime tool boundary."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260809_0009"
down_revision = "20260809_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_tool_effects",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("task_id", sa.String(96), nullable=False),
        sa.Column("effect_id", sa.String(160), nullable=False),
        sa.Column("tool_name", sa.String(160), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("artifact_json", sa.JSON(), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("task_id", "effect_id", name="uq_runtime_tool_effect"),
    )
    for column in ("task_id", "effect_id", "status"):
        op.create_index(f"ix_runtime_tool_effects_{column}", "runtime_tool_effects", [column])


def downgrade() -> None:
    op.drop_table("runtime_tool_effects")
