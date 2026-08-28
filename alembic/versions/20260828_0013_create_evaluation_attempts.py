"""创建真实模型评测尝试生命周期。

版本标识：0013_evaluation_attempts
前置版本：0012_evaluation_experiments
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0013_evaluation_attempts"
down_revision: str | None = "0012_evaluation_experiments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增调用前落库、成功或失败后收敛的评测尝试表。"""

    op.create_table(
        "evaluation_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "sequence",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("evaluation_case_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("experiment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repetition_index", sa.Integer(), nullable=False),
        sa.Column("scenario_id", sa.String(length=100), nullable=False),
        sa.Column("mode", sa.String(length=30), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("budget_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("telemetry", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("failure_category", sa.String(length=50), nullable=True),
        sa.Column("failure_message", sa.String(length=500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mode IN ('single_agent', 'multi_agent')",
            name="ck_evaluation_attempts_mode",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_evaluation_attempts_status",
        ),
        sa.CheckConstraint(
            "failure_category IS NULL OR failure_category IN "
            "('model_request_error', 'tool_error', 'timeout', "
            "'structured_output_error', 'safety_rejection', 'budget_exceeded')",
            name="ck_evaluation_attempts_failure_category",
        ),
        sa.CheckConstraint(
            "repetition_index BETWEEN 1 AND 1000",
            name="ck_evaluation_attempts_repetition_index",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(budget_snapshot) = 'object'",
            name="ck_evaluation_attempts_budget_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(telemetry) = 'object'",
            name="ck_evaluation_attempts_telemetry_object",
        ),
        sa.CheckConstraint(
            "result_snapshot IS NULL OR jsonb_typeof(result_snapshot) = 'object'",
            name="ck_evaluation_attempts_result_object",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND result_snapshot IS NULL "
            "AND failure_category IS NULL AND failure_message IS NULL) OR "
            "(status = 'succeeded' AND finished_at IS NOT NULL AND result_snapshot IS NOT NULL "
            "AND failure_category IS NULL AND failure_message IS NULL) OR "
            "(status = 'failed' AND finished_at IS NOT NULL AND result_snapshot IS NULL "
            "AND failure_category IS NOT NULL AND failure_message IS NOT NULL)",
            name="ck_evaluation_attempts_terminal_shape",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_evaluation_attempts_time_order",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_case_set_id"],
            ["evaluation_cases.id"],
            name="fk_evaluation_attempts_case_set",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["evaluation_experiments.id"],
            name="fk_evaluation_attempts_experiment",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sequence"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint(
            "experiment_id",
            "repetition_index",
            "scenario_id",
            "mode",
            name="uq_evaluation_attempts_experiment_slot",
        ),
    )
    op.create_index(
        "ix_evaluation_attempts_sequence",
        "evaluation_attempts",
        ["sequence"],
        unique=False,
    )


def downgrade() -> None:
    """移除真实模型评测尝试表。"""

    op.drop_index("ix_evaluation_attempts_sequence", table_name="evaluation_attempts")
    op.drop_table("evaluation_attempts")
