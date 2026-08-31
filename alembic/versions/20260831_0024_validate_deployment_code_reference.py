"""校验部署事实中的代码仓库与 Commit SHA。

版本标识：0024_deployment_code_reference
前置版本：0023_investigation_attempts
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024_deployment_code_reference"
down_revision: str | None = "0023_investigation_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """可选代码引用一旦出现，就在数据库层强制完整、有限且不可伪装。"""

    op.create_check_constraint(
        "ck_change_records_code_reference",
        "change_records",
        "(change_type = 'deployment' AND (NOT (details ? 'code_reference') OR ("
        "jsonb_typeof(details -> 'code_reference') = 'object' AND "
        "jsonb_typeof(details #> '{code_reference,repository}') = 'string' AND "
        "jsonb_typeof(details #> '{code_reference,commit_sha}') = 'string' AND "
        "length(details #>> '{code_reference,repository}') BETWEEN 3 AND 140 AND "
        "(details #>> '{code_reference,repository}') "
        "~ '^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$' AND "
        "split_part(details #>> '{code_reference,repository}', '/', 2) "
        "NOT IN ('.', '..') AND "
        "(details #>> '{code_reference,commit_sha}') ~ '^[0-9a-f]{40,64}$'"
        "))) OR (change_type <> 'deployment' AND NOT (details ? 'code_reference'))",
    )


def downgrade() -> None:
    """只移除额外校验，不删除或改写任何变更事实。"""

    op.drop_constraint(
        "ck_change_records_code_reference",
        "change_records",
        type_="check",
    )
