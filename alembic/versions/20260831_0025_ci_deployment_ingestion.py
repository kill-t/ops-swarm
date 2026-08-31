"""增加受认证 CI/CD 部署来源与持久来源身份。

版本标识：0025_ci_deployment_ingestion
前置版本：0024_deployment_code_reference
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_ci_deployment_ingestion"
down_revision: str | None = "0024_deployment_code_reference"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保留历史本地身份，并只允许 CI 来源写入完整部署事实。"""

    op.add_column(
        "change_records",
        sa.Column("source_identity", sa.String(length=64), nullable=True),
    )
    op.execute(
        "UPDATE change_records SET source_identity = 'local-admin' WHERE source_identity IS NULL"
    )
    op.alter_column("change_records", "source_identity", nullable=False)

    op.drop_constraint("ck_change_records_source", "change_records", type_="check")
    op.create_check_constraint(
        "ck_change_records_source",
        "change_records",
        "source IN ('fault-lab-change-simulator', 'ci-deployment-ingestion')",
    )
    op.create_check_constraint(
        "ck_change_records_source_identity",
        "change_records",
        "(source = 'fault-lab-change-simulator' AND source_identity = 'local-admin') OR "
        "(source = 'ci-deployment-ingestion' AND source_identity <> 'local-admin' AND "
        "source_identity ~ '^[a-z0-9][a-z0-9._-]{0,63}$')",
    )
    op.create_check_constraint(
        "ck_change_records_ci_deployment",
        "change_records",
        "source <> 'ci-deployment-ingestion' OR "
        "(change_type = 'deployment' AND details ? 'code_reference')",
    )


def downgrade() -> None:
    """存在 CI/CD 审计事实时拒绝降级，避免静默丢失来源身份。"""

    connection = op.get_bind()
    ci_record_count = connection.execute(
        sa.text("SELECT count(*) FROM change_records WHERE source = 'ci-deployment-ingestion'")
    ).scalar_one()
    if ci_record_count:
        raise RuntimeError("存在 CI/CD 部署事实，不能降级并丢失来源身份")

    op.drop_constraint("ck_change_records_ci_deployment", "change_records", type_="check")
    op.drop_constraint("ck_change_records_source_identity", "change_records", type_="check")
    op.drop_constraint("ck_change_records_source", "change_records", type_="check")
    op.create_check_constraint(
        "ck_change_records_source",
        "change_records",
        "source = 'fault-lab-change-simulator'",
    )
    op.drop_column("change_records", "source_identity")
