"""为跨事故未完成行动项治理查询增加部分索引。

版本标识：0029_follow_up_governance_idx
前置版本：0028_create_report_action_items
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0029_follow_up_governance_idx"
down_revision: str | None = "0028_create_report_action_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """只为 open 行动项建立截止时间和负责人筛选索引。"""

    op.create_index(
        "ix_report_action_items_open_due",
        "report_action_items",
        ["due_at"],
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_report_action_items_open_owner_due",
        "report_action_items",
        ["owner_id", "due_at"],
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    """删除跨事故治理查询专用索引。"""

    op.drop_index(
        "ix_report_action_items_open_owner_due",
        table_name="report_action_items",
    )
    op.drop_index(
        "ix_report_action_items_open_due",
        table_name="report_action_items",
    )
