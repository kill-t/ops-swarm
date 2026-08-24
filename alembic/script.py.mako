"""${message}

版本标识：${up_revision}
前置版本：${down_revision | comma,n}
创建时间：${create_date}
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """升级数据库结构。"""

    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """回退数据库结构。"""

    ${downgrades if downgrades else "pass"}
