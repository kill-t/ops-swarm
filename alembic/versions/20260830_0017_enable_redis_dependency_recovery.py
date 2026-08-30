"""开放 Redis 依赖恢复计划、执行与验证审计类型。

版本标识：0017_redis_dependency_recovery
前置版本：0016_verification_work_items
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_redis_dependency_recovery"
down_revision: str | None = "0016_verification_work_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """扩展 Redis 依赖动作、执行器、验证器和样本白名单。"""

    op.drop_constraint("ck_action_steps_action_type", "action_steps", type_="check")
    op.create_check_constraint(
        "ck_action_steps_action_type",
        "action_steps",
        "action_type IN ('rollback_release', 'rollback_configuration', 'restore_dependency')",
    )
    op.drop_constraint("ck_action_steps_target_type", "action_steps", type_="check")
    op.create_check_constraint(
        "ck_action_steps_target_type",
        "action_steps",
        "target_type IN ('service', 'dependency')",
    )
    op.drop_constraint(
        "ck_operation_executions_executor",
        "operation_executions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_operation_executions_executor",
        "operation_executions",
        "executor_name IN ("
        "'local_fault_lab_release_500_v1', "
        "'local_fault_lab_database_pool_v1', "
        "'local_fault_lab_redis_dependency_v1'"
        ")",
    )
    op.drop_constraint("ck_verification_runs_verifier", "verification_runs", type_="check")
    op.create_check_constraint(
        "ck_verification_runs_verifier",
        "verification_runs",
        "verifier_name IN ("
        "'local_order_http_5xx_ratio_v1', "
        "'local_database_pool_saturation_v1', "
        "'local_redis_unavailable_ratio_v1'"
        ")",
    )
    op.drop_constraint("ck_verification_runs_rule_kind", "verification_runs", type_="check")
    op.create_check_constraint(
        "ck_verification_runs_rule_kind",
        "verification_runs",
        "signal IN ("
        "'http_5xx_ratio', 'database_pool_saturation', 'redis_unavailable_ratio'"
        ") AND operator = 'less_than'",
    )
    op.drop_constraint("ck_verification_samples_signal", "verification_samples", type_="check")
    op.create_check_constraint(
        "ck_verification_samples_signal",
        "verification_samples",
        "signal IN ('http_5xx_ratio', 'database_pool_saturation', 'redis_unavailable_ratio')",
    )


def downgrade() -> None:
    """存在 Redis 恢复审计事实时拒绝收窄白名单。"""

    connection = op.get_bind()
    has_redis_history = connection.scalar(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM action_steps WHERE action_type = 'restore_dependency'"
            ") OR EXISTS ("
            "SELECT 1 FROM operation_executions "
            "WHERE executor_name = 'local_fault_lab_redis_dependency_v1'"
            ") OR EXISTS ("
            "SELECT 1 FROM verification_runs "
            "WHERE verifier_name = 'local_redis_unavailable_ratio_v1' "
            "OR signal = 'redis_unavailable_ratio'"
            ") OR EXISTS ("
            "SELECT 1 FROM verification_samples WHERE signal = 'redis_unavailable_ratio'"
            ")"
        )
    )
    if has_redis_history:
        raise RuntimeError("存在 Redis 依赖恢复历史，不能安全降级到旧白名单")

    op.drop_constraint("ck_verification_samples_signal", "verification_samples", type_="check")
    op.create_check_constraint(
        "ck_verification_samples_signal",
        "verification_samples",
        "signal IN ('http_5xx_ratio', 'database_pool_saturation')",
    )
    op.drop_constraint("ck_verification_runs_rule_kind", "verification_runs", type_="check")
    op.create_check_constraint(
        "ck_verification_runs_rule_kind",
        "verification_runs",
        "signal IN ('http_5xx_ratio', 'database_pool_saturation') AND operator = 'less_than'",
    )
    op.drop_constraint("ck_verification_runs_verifier", "verification_runs", type_="check")
    op.create_check_constraint(
        "ck_verification_runs_verifier",
        "verification_runs",
        "verifier_name IN ('local_order_http_5xx_ratio_v1', 'local_database_pool_saturation_v1')",
    )
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
    op.drop_constraint("ck_action_steps_target_type", "action_steps", type_="check")
    op.create_check_constraint(
        "ck_action_steps_target_type",
        "action_steps",
        "target_type IN ('service')",
    )
    op.drop_constraint("ck_action_steps_action_type", "action_steps", type_="check")
    op.create_check_constraint(
        "ck_action_steps_action_type",
        "action_steps",
        "action_type IN ('rollback_release', 'rollback_configuration')",
    )
