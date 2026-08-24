"""增加多 Agent 调查模式与持久任务类型。

版本标识：0003_multi_agent_worker
前置版本：0002_investigation_queue
创建时间：2026-08-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_multi_agent_worker"
down_revision: str | None = "0002_investigation_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """扩展运行模式，并要求多 Agent 运行关联图线程。"""

    op.drop_constraint("ck_investigation_runs_mode", "investigation_runs", type_="check")
    op.create_check_constraint(
        "ck_investigation_runs_mode",
        "investigation_runs",
        "mode IN ('single_agent', 'multi_agent')",
    )
    op.create_check_constraint(
        "ck_investigation_runs_multi_thread",
        "investigation_runs",
        "mode <> 'multi_agent' OR graph_thread_id IS NOT NULL",
    )
    op.drop_constraint("ck_work_items_kind", "work_items", type_="check")
    op.create_check_constraint(
        "ck_work_items_kind",
        "work_items",
        "kind IN ('run_single_agent_investigation', 'run_multi_agent_investigation')",
    )


def downgrade() -> None:
    """只有不存在多 Agent 历史数据时才能回退到单 Agent schema。"""

    op.drop_constraint("ck_work_items_kind", "work_items", type_="check")
    op.create_check_constraint(
        "ck_work_items_kind",
        "work_items",
        "kind IN ('run_single_agent_investigation')",
    )
    op.drop_constraint(
        "ck_investigation_runs_multi_thread",
        "investigation_runs",
        type_="check",
    )
    op.drop_constraint("ck_investigation_runs_mode", "investigation_runs", type_="check")
    op.create_check_constraint(
        "ck_investigation_runs_mode",
        "investigation_runs",
        "mode IN ('single_agent')",
    )
