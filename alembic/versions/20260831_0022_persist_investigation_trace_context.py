"""持久化调查任务的 W3C Trace Context。

版本标识：0022_investigation_trace_context
前置版本：0021_investigation_metric_scope
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0022_investigation_trace_context"
down_revision: str | None = "0021_investigation_metric_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """允许新任务保存最小 W3C 父上下文，历史任务保持空值。"""

    op.add_column(
        "work_items",
        sa.Column("trace_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "ck_work_items_trace_context_object",
        "work_items",
        "trace_context IS NULL OR jsonb_typeof(trace_context) = 'object'",
    )


def downgrade() -> None:
    """移除追踪上下文，不修改任务业务状态。"""

    op.drop_constraint(
        "ck_work_items_trace_context_object",
        "work_items",
        type_="check",
    )
    op.drop_column("work_items", "trace_context")
