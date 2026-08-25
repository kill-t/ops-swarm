"""创建绑定精确计划版本的审批请求。

版本标识：0007_approval_requests
前置版本：0006_action_plans
创建时间：2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_approval_requests"
down_revision: str | None = "0006_action_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """允许计划进入待审批，并保存有有效期的 pending 请求。"""

    op.drop_constraint("ck_action_plans_status", "action_plans", type_="check")
    op.create_check_constraint(
        "ck_action_plans_status",
        "action_plans",
        "status IN ('draft', 'awaiting_approval')",
    )
    op.create_unique_constraint(
        "uq_action_plans_id_version",
        "action_plans",
        ["id", "version"],
    )
    op.create_table(
        "approval_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_plan_version", sa.Integer(), nullable=False),
        sa.Column("plan_content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_by_type", sa.String(length=20), nullable=False),
        sa.Column("requested_by_id", sa.String(length=100), nullable=False),
        sa.Column("request_reason", sa.String(length=500), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending')", name="ck_approval_requests_status"),
        sa.CheckConstraint(
            "requested_by_type IN ('human')",
            name="ck_approval_requests_requester_type",
        ),
        sa.CheckConstraint(
            "plan_content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_approval_requests_content_hash",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_approval_requests_expiry",
        ),
        sa.ForeignKeyConstraint(
            ["action_plan_id", "action_plan_version"],
            ["action_plans.id", "action_plans.version"],
            name="fk_approval_requests_exact_plan_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "action_plan_id",
            name="uq_approval_requests_action_plan",
        ),
    )
    op.create_index(
        "ix_approval_requests_status_expires",
        "approval_requests",
        ["status", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    """删除审批请求，并把尚未决定的计划恢复为草案状态。"""

    op.drop_index("ix_approval_requests_status_expires", table_name="approval_requests")
    op.drop_table("approval_requests")
    op.drop_constraint("uq_action_plans_id_version", "action_plans", type_="unique")
    op.drop_constraint("ck_action_plans_status", "action_plans", type_="check")
    op.execute("UPDATE action_plans SET status = 'draft' WHERE status = 'awaiting_approval'")
    op.create_check_constraint(
        "ck_action_plans_status",
        "action_plans",
        "status IN ('draft')",
    )
