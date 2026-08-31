"""增加版本化来源契约与不可变容器镜像摘要。

版本标识：0026_deployment_artifact_digest
前置版本：0025_ci_deployment_ingestion
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026_deployment_artifact_digest"
down_revision: str | None = "0025_ci_deployment_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保留历史 v1 事实，并让新 CI v2 事实强制包含镜像摘要。"""

    op.add_column(
        "change_records",
        sa.Column("source_contract_version", sa.SmallInteger(), nullable=True),
    )
    op.execute("UPDATE change_records SET source_contract_version = 1")
    op.alter_column("change_records", "source_contract_version", nullable=False)

    op.create_check_constraint(
        "ck_change_records_source_contract",
        "change_records",
        "(source = 'fault-lab-change-simulator' AND source_contract_version = 1 AND "
        "NOT (details ? 'artifact_reference')) OR "
        "(source = 'ci-deployment-ingestion' AND "
        "((source_contract_version = 1 AND NOT (details ? 'artifact_reference')) OR "
        "source_contract_version = 2))",
    )
    op.create_check_constraint(
        "ck_change_records_ci_artifact_v2",
        "change_records",
        "source <> 'ci-deployment-ingestion' OR source_contract_version = 1 OR "
        "(source_contract_version = 2 AND change_type = 'deployment' AND "
        "details ? 'code_reference' AND details ? 'artifact_reference')",
    )
    op.create_check_constraint(
        "ck_change_records_artifact_reference",
        "change_records",
        "(change_type = 'deployment' AND (NOT (details ? 'artifact_reference') OR ("
        "jsonb_typeof(details -> 'artifact_reference') = 'object' AND "
        "(details -> 'artifact_reference') - 'image_repository' - 'image_digest' "
        "= '{}'::jsonb AND "
        "jsonb_typeof(details #> '{artifact_reference,image_repository}') = 'string' AND "
        "jsonb_typeof(details #> '{artifact_reference,image_digest}') = 'string' AND "
        "length(details #>> '{artifact_reference,image_repository}') BETWEEN 3 AND 255 AND "
        "(details #>> '{artifact_reference,image_repository}') "
        "~ '^[a-z0-9][a-z0-9._-]*"
        "(?::([1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|"
        "65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))?"
        "(/[a-z0-9][a-z0-9._-]*)+$' AND "
        "(details #>> '{artifact_reference,image_digest}') ~ '^sha256:[0-9a-f]{64}$'"
        "))) OR (change_type <> 'deployment' AND NOT (details ? 'artifact_reference'))",
    )


def downgrade() -> None:
    """存在 v2 审计事实时拒绝丢失来源契约版本。"""

    connection = op.get_bind()
    v2_record_count = connection.execute(
        sa.text("SELECT count(*) FROM change_records WHERE source_contract_version = 2")
    ).scalar_one()
    if v2_record_count:
        raise RuntimeError("存在 CI/CD v2 部署事实，不能降级并丢失来源契约版本")

    op.drop_constraint("ck_change_records_artifact_reference", "change_records", type_="check")
    op.drop_constraint("ck_change_records_ci_artifact_v2", "change_records", type_="check")
    op.drop_constraint("ck_change_records_source_contract", "change_records", type_="check")
    op.drop_column("change_records", "source_contract_version")
