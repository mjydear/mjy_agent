"""Create operation plan and approval lifecycle tables."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260719_0004"
down_revision = "20260719_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operation_plans",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("task_id", sa.String(80)),
        sa.Column("environment_id", sa.String(120), nullable=False),
        sa.Column("action_type", sa.String(120), nullable=False),
        sa.Column("resource_kind", sa.String(80), nullable=False),
        sa.Column("resource_name", sa.String(160), nullable=False),
        sa.Column("namespace", sa.String(120), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("required_scope", sa.String(120), nullable=False),
        sa.Column("plan_hash", sa.String(128), nullable=False),
        sa.Column("canonical_json", sa.JSON(), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("preconditions_json", sa.JSON(), nullable=False),
        sa.Column("postconditions_json", sa.JSON(), nullable=False),
        sa.Column("rollback_json", sa.JSON(), nullable=False),
        sa.Column("dry_run_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_by", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "plan_hash", name="uq_operation_plan_hash"),
    )
    op.create_index("ix_operation_plans_tenant_id", "operation_plans", ["tenant_id"])
    op.create_index("ix_operation_plans_task_id", "operation_plans", ["task_id"])
    op.create_index(
        "ix_operation_plans_environment_id", "operation_plans", ["environment_id"]
    )
    op.create_index("ix_operation_plans_plan_hash", "operation_plans", ["plan_hash"])
    op.create_index("ix_operation_plans_status", "operation_plans", ["status"])
    op.create_index("ix_operation_plans_expires_at", "operation_plans", ["expires_at"])

    op.create_table(
        "approvals",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("tenant_id", sa.String(120), nullable=False),
        sa.Column("plan_id", sa.String(80), nullable=False),
        sa.Column("plan_hash", sa.String(128), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("requested_by", sa.String(160), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(160)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision_note", sa.Text()),
        sa.Column("scopes_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_approvals_tenant_id", "approvals", ["tenant_id"])
    op.create_index("ix_approvals_plan_id", "approvals", ["plan_id"])
    op.create_index("ix_approvals_plan_hash", "approvals", ["plan_hash"])
    op.create_index("ix_approvals_status", "approvals", ["status"])
    op.create_index("ix_approvals_requested_at", "approvals", ["requested_at"])
    op.create_index("ix_approvals_expires_at", "approvals", ["expires_at"])


def downgrade() -> None:
    op.drop_table("approvals")
    op.drop_table("operation_plans")
