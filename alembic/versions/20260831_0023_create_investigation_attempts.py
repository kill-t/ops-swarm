"""创建调查 Worker 尝试历史。

版本标识：0023_investigation_attempts
前置版本：0022_investigation_trace_context
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0023_investigation_attempts"
down_revision: str | None = "0022_investigation_trace_context"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为每次 Worker 租约领取建立可结算历史。"""

    op.create_table(
        "investigation_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("worker_id", sa.String(length=100), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=True),
        sa.Column("input_cost_per_million_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("output_cost_per_million_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("model_request_count", sa.Integer(), nullable=False),
        sa.Column("usage_complete", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("will_retry", sa.Boolean(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'lease_expired')",
            name="ck_investigation_attempts_status",
        ),
        sa.CheckConstraint(
            "attempt_number >= 1",
            name="ck_investigation_attempts_number",
        ),
        sa.CheckConstraint(
            "model_request_count >= 0 AND input_tokens >= 0 AND output_tokens >= 0 "
            "AND total_tokens = input_tokens + output_tokens",
            name="ck_investigation_attempts_usage",
        ),
        sa.CheckConstraint(
            "usage_complete OR (NOT usage_complete AND estimated_cost_usd IS NULL)",
            name="ck_investigation_attempts_usage_complete",
        ),
        sa.CheckConstraint(
            "(input_cost_per_million_usd IS NULL AND output_cost_per_million_usd IS NULL) OR "
            "(input_cost_per_million_usd IS NOT NULL "
            "AND output_cost_per_million_usd IS NOT NULL "
            "AND input_cost_per_million_usd >= 0 AND output_cost_per_million_usd >= 0)",
            name="ck_investigation_attempts_pricing_pair",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'lease_expired') OR "
            "(usage_complete AND input_cost_per_million_usd IS NOT NULL "
            "AND estimated_cost_usd >= 0) OR "
            "(input_cost_per_million_usd IS NULL AND estimated_cost_usd IS NULL) OR "
            "(NOT usage_complete AND estimated_cost_usd IS NULL)",
            name="ck_investigation_attempts_cost_shape",
        ),
        sa.CheckConstraint(
            "status <> 'lease_expired' OR (NOT usage_complete AND estimated_cost_usd IS NULL)",
            name="ck_investigation_attempts_expired_usage",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND duration_ms IS NULL "
            "AND will_retry IS NULL AND error_code IS NULL AND error_message IS NULL "
            "AND estimated_cost_usd IS NULL) OR "
            "(status = 'succeeded' AND finished_at IS NOT NULL AND duration_ms >= 0 "
            "AND will_retry = false AND error_code IS NULL AND error_message IS NULL) OR "
            "(status IN ('failed', 'lease_expired') AND finished_at IS NOT NULL "
            "AND duration_ms >= 0 AND will_retry IS NOT NULL "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL)",
            name="ck_investigation_attempts_terminal_shape",
        ),
        sa.CheckConstraint(
            "started_at <= lease_expires_at AND (finished_at IS NULL OR finished_at >= started_at)",
            name="ck_investigation_attempts_time_order",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_run_id"],
            ["investigation_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["work_item_id"],
            ["work_items.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lease_token"),
        sa.UniqueConstraint(
            "work_item_id",
            "attempt_number",
            name="uq_investigation_attempts_work_number",
        ),
    )
    op.create_index(
        "ix_investigation_attempts_run_number",
        "investigation_attempts",
        ["investigation_run_id", "attempt_number"],
        unique=False,
    )


def downgrade() -> None:
    """回退时只移除尝试历史，不改写原任务。"""

    op.drop_index(
        "ix_investigation_attempts_run_number",
        table_name="investigation_attempts",
    )
    op.drop_table("investigation_attempts")
