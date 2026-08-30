"""开放 Prometheus 恢复验证器审计名称。

版本标识：0018_prometheus_verification
前置版本：0017_redis_dependency_recovery
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_prometheus_verification"
down_revision: str | None = "0017_redis_dependency_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保留历史本地探针名称，并允许新运行记录 Prometheus 数据源。"""

    op.drop_constraint("ck_verification_runs_verifier", "verification_runs", type_="check")
    op.create_check_constraint(
        "ck_verification_runs_verifier",
        "verification_runs",
        "verifier_name IN ("
        "'local_order_http_5xx_ratio_v1', "
        "'local_database_pool_saturation_v1', "
        "'local_redis_unavailable_ratio_v1', "
        "'prometheus_order_http_5xx_ratio_v1', "
        "'prometheus_database_pool_saturation_v1', "
        "'prometheus_redis_unavailable_ratio_v1'"
        ")",
    )


def downgrade() -> None:
    """存在 Prometheus 验证审计事实时拒绝收窄约束。"""

    connection = op.get_bind()
    has_prometheus_history = connection.scalar(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM verification_runs "
            "WHERE verifier_name IN ("
            "'prometheus_order_http_5xx_ratio_v1', "
            "'prometheus_database_pool_saturation_v1', "
            "'prometheus_redis_unavailable_ratio_v1'"
            ")"
            ")"
        )
    )
    if has_prometheus_history:
        raise RuntimeError("存在 Prometheus 恢复验证历史，不能安全降级到旧白名单")

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
