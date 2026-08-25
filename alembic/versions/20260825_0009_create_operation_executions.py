"""创建带租约和幂等键的白名单操作执行记录。

版本标识：0009_operation_executions
前置版本：0008_approval_decisions
创建时间：2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_operation_executions"
down_revision: str | None = "0008_approval_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保存可恢复执行，并用复合外键绑定批准、计划和步骤。"""

    op.create_unique_constraint(
        "uq_approval_requests_exact_plan_binding",
        "approval_requests",
        ["id", "action_plan_id", "action_plan_version"],
    )
    op.create_unique_constraint(
        "uq_action_steps_id_plan",
        "action_steps",
        ["id", "action_plan_id"],
    )
    op.create_table(
        "operation_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_plan_version", sa.Integer(), nullable=False),
        sa.Column("action_step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("executor_name", sa.String(length=100), nullable=False),
        sa.Column("incident_version_before", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_operation_executions_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 1",
            name="ck_operation_executions_attempt_count",
        ),
        sa.CheckConstraint(
            "executor_name IN ('local_fault_lab_release_500_v1')",
            name="ck_operation_executions_executor",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL AND locked_until IS NOT NULL "
            "AND result IS NULL AND error_code IS NULL AND error_message IS NULL "
            "AND completed_at IS NULL) OR "
            "(status = 'succeeded' AND lease_token IS NULL AND locked_until IS NULL "
            "AND jsonb_typeof(result) = 'object' AND error_code IS NULL "
            "AND error_message IS NULL AND completed_at IS NOT NULL) OR "
            "(status = 'failed' AND lease_token IS NULL AND locked_until IS NULL "
            "AND result IS NULL AND error_code IS NOT NULL AND error_message IS NOT NULL "
            "AND completed_at IS NOT NULL)",
            name="ck_operation_executions_lifecycle",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_operation_executions_completion_time",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_operation_executions_incident",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approval_request_id", "action_plan_id", "action_plan_version"],
            [
                "approval_requests.id",
                "approval_requests.action_plan_id",
                "approval_requests.action_plan_version",
            ],
            name="fk_operation_executions_exact_approval",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_plan_id", "action_plan_version"],
            ["action_plans.id", "action_plans.version"],
            name="fk_operation_executions_exact_plan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["action_step_id", "action_plan_id"],
            ["action_steps.id", "action_steps.action_plan_id"],
            name="fk_operation_executions_step_plan",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_operation_executions_idempotency_key",
        ),
    )
    op.create_index(
        "uq_operation_executions_running_step",
        "operation_executions",
        ["action_step_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "uq_operation_executions_succeeded_step",
        "operation_executions",
        ["action_step_id"],
        unique=True,
        postgresql_where=sa.text("status = 'succeeded'"),
    )
    op.create_index(
        "ix_operation_executions_incident_status",
        "operation_executions",
        ["incident_id", "status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """仅在没有执行审计记录时移除本切片结构。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM operation_executions) THEN
                RAISE EXCEPTION '存在操作执行记录，拒绝执行有损降级';
            END IF;
        END
        $$
        """
    )
    op.drop_index(
        "ix_operation_executions_incident_status",
        table_name="operation_executions",
    )
    op.drop_index(
        "uq_operation_executions_succeeded_step",
        table_name="operation_executions",
    )
    op.drop_index(
        "uq_operation_executions_running_step",
        table_name="operation_executions",
    )
    op.drop_table("operation_executions")
    op.drop_constraint("uq_action_steps_id_plan", "action_steps", type_="unique")
    op.drop_constraint(
        "uq_approval_requests_exact_plan_binding",
        "approval_requests",
        type_="unique",
    )
