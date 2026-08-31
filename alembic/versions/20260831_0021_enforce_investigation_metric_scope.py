"""约束冻结评测指标与现场运行统计边界。

版本标识：0021_investigation_metric_scope
前置版本：0020_investigation_data_mode
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021_investigation_metric_scope"
down_revision: str | None = "0020_investigation_data_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """禁止现场结果携带冻结准确率，也禁止冻结成功运行遗漏指标。"""

    op.create_check_constraint(
        "ck_investigation_runs_metric_scope",
        "investigation_runs",
        "status <> 'succeeded' OR "
        "(data_source_mode = 'frozen' AND metrics IS NOT NULL) OR "
        "(data_source_mode = 'live' AND metrics IS NULL)",
    )


def downgrade() -> None:
    """只移除评分范围约束，不改写已经保存的运行结果。"""

    op.drop_constraint(
        "ck_investigation_runs_metric_scope",
        "investigation_runs",
        type_="check",
    )
