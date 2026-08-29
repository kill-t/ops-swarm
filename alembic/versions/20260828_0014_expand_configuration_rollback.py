"""扩展数据库连接池配置回滚计划动作。

版本标识：0014_configuration_rollback
前置版本：0013_evaluation_attempts
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_configuration_rollback"
down_revision: str | None = "0013_evaluation_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """允许持久化经过参数校验的配置回滚计划步骤。"""

    op.drop_constraint("ck_action_steps_action_type", "action_steps", type_="check")
    op.create_check_constraint(
        "ck_action_steps_action_type",
        "action_steps",
        "action_type IN ('rollback_release', 'rollback_configuration')",
    )


def downgrade() -> None:
    """只有不存在配置回滚历史时才恢复旧动作约束。"""

    connection = op.get_bind()
    has_configuration_steps = connection.scalar(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM action_steps WHERE action_type = 'rollback_configuration'"
            ")"
        )
    )
    if has_configuration_steps:
        raise RuntimeError("存在配置回滚计划历史，不能安全降级到仅支持版本回滚的结构")
    op.drop_constraint("ck_action_steps_action_type", "action_steps", type_="check")
    op.create_check_constraint(
        "ck_action_steps_action_type",
        "action_steps",
        "action_type IN ('rollback_release')",
    )
