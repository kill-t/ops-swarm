"""创建持久恢复验证调度任务。

版本标识：0016_verification_work_items
前置版本：0015_configuration_execution
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016_verification_work_items"
down_revision: str | None = "0015_configuration_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建租约任务，并为已有验证运行补齐可恢复调度状态。"""

    op.create_table(
        "verification_work_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("verification_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("next_sample_sequence", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(length=100), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("consecutive_failure_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("last_error_message", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed')",
            name="ck_verification_work_items_status",
        ),
        sa.CheckConstraint(
            "next_sample_sequence >= 1 AND attempt_count >= 0 AND consecutive_failure_count >= 0",
            name="ck_verification_work_items_counts",
        ),
        sa.CheckConstraint(
            "(last_error_code IS NULL AND last_error_message IS NULL) OR "
            "(last_error_code IS NOT NULL AND last_error_message IS NOT NULL)",
            name="ck_verification_work_items_error_shape",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND locked_by IS NOT NULL AND locked_until IS NOT NULL "
            "AND lease_token IS NOT NULL AND locked_until > updated_at) OR "
            "(status <> 'running' AND locked_by IS NULL AND locked_until IS NULL "
            "AND lease_token IS NULL)",
            name="ck_verification_work_items_lease_shape",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL) OR "
            "(status <> 'completed' AND completed_at IS NULL)",
            name="ck_verification_work_items_completion_shape",
        ),
        sa.CheckConstraint(
            "created_at <= updated_at AND created_at <= available_at "
            "AND (completed_at IS NULL OR completed_at >= created_at)",
            name="ck_verification_work_items_time_order",
        ),
        sa.ForeignKeyConstraint(
            ["verification_run_id"],
            ["verification_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("verification_run_id"),
    )
    op.create_index(
        "ix_verification_work_items_pending_claim",
        "verification_work_items",
        ["available_at", "created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_verification_work_items_expired_lease",
        "verification_work_items",
        ["locked_until"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.execute(
        """
        INSERT INTO verification_work_items (
            id,
            verification_run_id,
            status,
            next_sample_sequence,
            available_at,
            locked_by,
            locked_until,
            lease_token,
            attempt_count,
            consecutive_failure_count,
            last_error_code,
            last_error_message,
            created_at,
            updated_at,
            completed_at
        )
        SELECT
            run.id,
            run.id,
            CASE WHEN run.status = 'running' THEN 'pending' ELSE 'completed' END,
            COALESCE(sample_count.total, 0) + 1,
            run.updated_at,
            NULL,
            NULL,
            NULL,
            0,
            0,
            NULL,
            NULL,
            run.created_at,
            run.updated_at,
            CASE
                WHEN run.status = 'running' THEN NULL
                ELSE COALESCE(run.completed_at, run.updated_at)
            END
        FROM verification_runs AS run
        LEFT JOIN (
            SELECT verification_run_id, COUNT(*) AS total
            FROM verification_samples
            GROUP BY verification_run_id
        ) AS sample_count ON sample_count.verification_run_id = run.id
        """
    )


def downgrade() -> None:
    """仍有未完成验证任务时拒绝丢失恢复能力。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM verification_work_items WHERE status <> 'completed'
            ) THEN
                RAISE EXCEPTION '存在未完成恢复验证任务，不能安全降级';
            END IF;
        END
        $$
        """
    )
    op.drop_index(
        "ix_verification_work_items_expired_lease",
        table_name="verification_work_items",
    )
    op.drop_index(
        "ix_verification_work_items_pending_claim",
        table_name="verification_work_items",
    )
    op.drop_table("verification_work_items")
