"""开放数据库连接池配置执行与验证审计类型。

版本标识：0015_configuration_execution
前置版本：0014_configuration_rollback
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_configuration_execution"
down_revision: str | None = "0014_configuration_rollback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """允许配置回滚执行器、验证器和饱和度样本进入审计表。"""

    op.drop_constraint(
        "ck_operation_executions_executor",
        "operation_executions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_operation_executions_executor",
        "operation_executions",
        "executor_name IN ('local_fault_lab_release_500_v1', 'local_fault_lab_database_pool_v1')",
    )
    op.drop_constraint("ck_verification_runs_verifier", "verification_runs", type_="check")
    op.create_check_constraint(
        "ck_verification_runs_verifier",
        "verification_runs",
        "verifier_name IN ('local_order_http_5xx_ratio_v1', 'local_database_pool_saturation_v1')",
    )
    op.drop_constraint("ck_verification_runs_rule_kind", "verification_runs", type_="check")
    op.create_check_constraint(
        "ck_verification_runs_rule_kind",
        "verification_runs",
        "signal IN ('http_5xx_ratio', 'database_pool_saturation') AND operator = 'less_than'",
    )
    op.drop_constraint("ck_verification_samples_signal", "verification_samples", type_="check")
    op.create_check_constraint(
        "ck_verification_samples_signal",
        "verification_samples",
        "signal IN ('http_5xx_ratio', 'database_pool_saturation')",
    )


def downgrade() -> None:
    """存在配置执行或验证审计时拒绝恢复旧白名单。"""

    connection = op.get_bind()
    has_configuration_history = connection.scalar(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM operation_executions "
            "WHERE executor_name = 'local_fault_lab_database_pool_v1'"
            ") OR EXISTS ("
            "SELECT 1 FROM verification_runs "
            "WHERE verifier_name = 'local_database_pool_saturation_v1' "
            "OR signal = 'database_pool_saturation'"
            ") OR EXISTS ("
            "SELECT 1 FROM verification_samples "
            "WHERE signal = 'database_pool_saturation'"
            ")"
        )
    )
    if has_configuration_history:
        raise RuntimeError("存在配置回滚执行或验证历史，不能安全降级到旧白名单")

    op.drop_constraint("ck_verification_samples_signal", "verification_samples", type_="check")
    op.create_check_constraint(
        "ck_verification_samples_signal",
        "verification_samples",
        "signal = 'http_5xx_ratio'",
    )
    op.drop_constraint("ck_verification_runs_rule_kind", "verification_runs", type_="check")
    op.create_check_constraint(
        "ck_verification_runs_rule_kind",
        "verification_runs",
        "signal = 'http_5xx_ratio' AND operator = 'less_than'",
    )
    op.drop_constraint("ck_verification_runs_verifier", "verification_runs", type_="check")
    op.create_check_constraint(
        "ck_verification_runs_verifier",
        "verification_runs",
        "verifier_name IN ('local_order_http_5xx_ratio_v1')",
    )
    op.drop_constraint(
        "ck_operation_executions_executor",
        "operation_executions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_operation_executions_executor",
        "operation_executions",
        "executor_name IN ('local_fault_lab_release_500_v1')",
    )
