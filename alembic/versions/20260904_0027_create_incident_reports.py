"""创建不可变事故复盘报告快照。

版本标识：0027_create_incident_reports
前置版本：0026_deployment_artifact_digest
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0027_create_incident_reports"
down_revision: str | None = "0026_deployment_artifact_digest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为每个已关闭 Incident 保存唯一的结构化报告快照。"""

    op.create_table(
        "incident_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=50), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "content",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schema_version = 'incident-report.v1'",
            name="ck_incident_reports_schema_version",
        ),
        sa.CheckConstraint(
            "incident_version >= 1",
            name="ck_incident_reports_version_positive",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_incident_reports_content_hash",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object'",
            name="ck_incident_reports_content_object",
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name="fk_incident_reports_incident_id_incidents",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incident_reports")),
        sa.UniqueConstraint("incident_id", name="uq_incident_reports_incident"),
    )


def downgrade() -> None:
    """删除事故复盘报告快照表。"""

    op.drop_table("incident_reports")
