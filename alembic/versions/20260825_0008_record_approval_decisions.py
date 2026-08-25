"""记录审批决定并收敛过期请求。

版本标识：0008_approval_decisions
前置版本：0007_approval_requests
创建时间：2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_approval_decisions"
down_revision: str | None = "0007_approval_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """扩展请求和计划终态，并增加决定审计字段。"""

    op.drop_constraint("ck_action_plans_status", "action_plans", type_="check")
    op.create_check_constraint(
        "ck_action_plans_status",
        "action_plans",
        "status IN ('draft', 'awaiting_approval', 'approved', 'rejected', 'expired')",
    )
    op.add_column(
        "approval_requests",
        sa.Column("decided_by_type", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("decided_by_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("decision_reason", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "approval_requests",
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint("ck_approval_requests_status", "approval_requests", type_="check")
    op.create_check_constraint(
        "ck_approval_requests_status",
        "approval_requests",
        "status IN ('pending', 'approved', 'rejected', 'expired')",
    )
    op.create_check_constraint(
        "ck_approval_requests_decision_audit",
        "approval_requests",
        "(status = 'pending' AND decided_by_type IS NULL "
        "AND decided_by_id IS NULL AND decision_reason IS NULL AND decided_at IS NULL) "
        "OR (status IN ('approved', 'rejected') AND decided_by_type = 'human' "
        "AND decided_by_id IS NOT NULL AND decision_reason IS NOT NULL "
        "AND decided_at IS NOT NULL) "
        "OR (status = 'expired' AND decided_by_type = 'system' "
        "AND decided_by_id IS NULL AND decision_reason IS NOT NULL "
        "AND decided_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_approval_requests_decision_time",
        "approval_requests",
        "decided_at IS NULL OR decided_at >= created_at",
    )


def downgrade() -> None:
    """仅在没有终态审批记录时恢复到请求创建模型。"""

    # 决定审计不可静默丢失；本地需要回退时应先显式处理这些业务数据。
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM approval_requests WHERE status <> 'pending') THEN
                RAISE EXCEPTION '存在终态审批记录，拒绝执行有损降级';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint(
        "ck_approval_requests_decision_time",
        "approval_requests",
        type_="check",
    )
    op.drop_constraint(
        "ck_approval_requests_decision_audit",
        "approval_requests",
        type_="check",
    )
    op.drop_constraint("ck_approval_requests_status", "approval_requests", type_="check")
    op.create_check_constraint(
        "ck_approval_requests_status",
        "approval_requests",
        "status IN ('pending')",
    )
    op.drop_column("approval_requests", "decided_at")
    op.drop_column("approval_requests", "decision_reason")
    op.drop_column("approval_requests", "decided_by_id")
    op.drop_column("approval_requests", "decided_by_type")
    op.drop_constraint("ck_action_plans_status", "action_plans", type_="check")
    op.create_check_constraint(
        "ck_action_plans_status",
        "action_plans",
        "status IN ('draft', 'awaiting_approval')",
    )
