"""创建逐次恢复验证运行与不可变样本。

版本标识：0010_verification_runs
前置版本：0009_operation_executions
创建时间：2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010_verification_runs"
down_revision: str | None = "0009_operation_executions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """冻结验证规则，并保存每次由服务端取得的探针样本。"""

    op.create_unique_constraint(
        "uq_operation_executions_verification_binding",
        "operation_executions",
        ["id", "incident_id", "action_step_id"],
    )
    op.create_table(
        "verification_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_execution_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("verifier_name", sa.String(length=100), nullable=False),
        sa.Column("incident_version_before", sa.Integer(), nullable=False),
        sa.Column("signal", sa.String(length=100), nullable=False),
        sa.Column("operator", sa.String(length=32), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("observation_window_seconds", sa.Integer(), nullable=False),
        sa.Column("required_successful_samples", sa.Integer(), nullable=False),
        sa.Column("successful_sample_count", sa.Integer(), nullable=False),
        sa.Column("first_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_message", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'passed', 'failed')",
            name="ck_verification_runs_status",
        ),
        sa.CheckConstraint(
            "verifier_name IN ('local_order_http_5xx_ratio_v1')",
            name="ck_verification_runs_verifier",
        ),
        sa.CheckConstraint(
            "signal = 'http_5xx_ratio' AND operator = 'less_than'",
            name="ck_verification_runs_rule_kind",
        ),
        sa.CheckConstraint(
            "threshold >= 0 AND threshold <= 1",
            name="ck_verification_runs_threshold",
        ),
        sa.CheckConstraint(
            "observation_window_seconds BETWEEN 10 AND 3600",
            name="ck_verification_runs_window",
        ),
        sa.CheckConstraint(
            "required_successful_samples BETWEEN 1 AND 100",
            name="ck_verification_runs_required_samples",
        ),
        sa.CheckConstraint(
            "successful_sample_count >= 0",
            name="ck_verification_runs_sample_count",
        ),
        sa.CheckConstraint(
            "(successful_sample_count = 0 AND first_success_at IS NULL) OR "
            "(successful_sample_count > 0 AND first_success_at IS NOT NULL)",
            name="ck_verification_runs_first_success",
        ),
        sa.CheckConstraint(
            "last_observed_at IS NULL OR last_observed_at >= created_at",
            name="ck_verification_runs_last_observed",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND completed_at IS NULL AND failure_code IS NULL "
            "AND failure_message IS NULL) OR "
            "(status = 'passed' AND completed_at IS NOT NULL AND failure_code IS NULL "
            "AND failure_message IS NULL AND successful_sample_count >= "
            "required_successful_samples AND first_success_at IS NOT NULL "
            "AND last_observed_at IS NOT NULL) OR "
            "(status = 'failed' AND completed_at IS NOT NULL AND failure_code IS NOT NULL "
            "AND failure_message IS NOT NULL)",
            name="ck_verification_runs_lifecycle",
        ),
        sa.CheckConstraint(
            "status <> 'passed' OR EXTRACT(EPOCH FROM "
            "(last_observed_at - first_success_at)) >= observation_window_seconds",
            name="ck_verification_runs_passed_window",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_verification_runs_completion_time",
        ),
        sa.ForeignKeyConstraint(
            ["operation_execution_id", "incident_id", "action_step_id"],
            [
                "operation_executions.id",
                "operation_executions.incident_id",
                "operation_executions.action_step_id",
            ],
            name="fk_verification_runs_exact_execution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operation_execution_id",
            name="uq_verification_runs_operation_execution",
        ),
    )
    op.create_index(
        "ix_verification_runs_incident_status",
        "verification_runs",
        ["incident_id", "status", "created_at"],
        unique=False,
    )
    op.create_table(
        "verification_samples",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("verification_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("signal", sa.String(length=100), nullable=False),
        sa.Column("observed_value", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('passed', 'failed', 'error')",
            name="ck_verification_samples_status",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_verification_samples_sequence"),
        sa.CheckConstraint(
            "signal = 'http_5xx_ratio'",
            name="ck_verification_samples_signal",
        ),
        sa.CheckConstraint(
            "threshold >= 0 AND threshold <= 1",
            name="ck_verification_samples_threshold",
        ),
        sa.CheckConstraint(
            "observed_value IS NULL OR (observed_value >= 0 AND observed_value <= 1)",
            name="ck_verification_samples_value",
        ),
        sa.CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_verification_samples_http_status",
        ),
        sa.CheckConstraint(
            "(status = 'passed' AND observed_value IS NOT NULL AND "
            "observed_value < threshold AND http_status IS NOT NULL AND error_code IS NULL "
            "AND error_message IS NULL) OR "
            "(status = 'failed' AND observed_value IS NOT NULL AND "
            "observed_value >= threshold AND http_status IS NOT NULL AND error_code IS NULL "
            "AND error_message IS NULL) OR "
            "(status = 'error' AND observed_value IS NULL AND error_code IS NOT NULL "
            "AND error_message IS NOT NULL)",
            name="ck_verification_samples_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["verification_run_id"],
            ["verification_runs.id"],
            name="fk_verification_samples_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "verification_run_id",
            "idempotency_key",
            name="uq_verification_samples_run_idempotency",
        ),
        sa.UniqueConstraint(
            "verification_run_id",
            "sequence",
            name="uq_verification_samples_run_sequence",
        ),
    )


def downgrade() -> None:
    """仅在没有验证审计时移除本切片结构。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM verification_runs)
               OR EXISTS (SELECT 1 FROM verification_samples) THEN
                RAISE EXCEPTION '存在恢复验证记录，拒绝执行有损降级';
            END IF;
        END
        $$
        """
    )
    op.drop_table("verification_samples")
    op.drop_index("ix_verification_runs_incident_status", table_name="verification_runs")
    op.drop_table("verification_runs")
    op.drop_constraint(
        "uq_operation_executions_verification_binding",
        "operation_executions",
        type_="unique",
    )
