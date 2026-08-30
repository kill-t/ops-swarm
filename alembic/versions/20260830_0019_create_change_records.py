"""创建不可变变更记录表。

版本标识：0019_change_records
前置版本：0018_prometheus_verification
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019_change_records"
down_revision: str | None = "0018_prometheus_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立只追加变更事实及其幂等、完整性和查询约束。"""

    op.create_table(
        "change_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("external_change_id", sa.String(length=100), nullable=False),
        sa.Column("service_name", sa.String(length=100), nullable=False),
        sa.Column("change_type", sa.String(length=32), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_change_records_content_hash",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(details) = 'object'",
            name="ck_change_records_details",
        ),
        sa.CheckConstraint(
            "source = 'fault-lab-change-simulator'",
            name="ck_change_records_source",
        ),
        sa.CheckConstraint(
            "service_name = 'order-service'",
            name="ck_change_records_service",
        ),
        sa.CheckConstraint(
            "occurred_at <= recorded_at",
            name="ck_change_records_timestamp_order",
        ),
        sa.CheckConstraint(
            "change_type IN ('deployment', 'configuration', 'dependency_maintenance')",
            name="ck_change_records_type",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source",
            "external_change_id",
            name="uq_change_records_source_external_id",
        ),
    )
    op.create_index(
        "ix_change_records_service_occurred",
        "change_records",
        ["service_name", sa.literal_column("occurred_at DESC"), sa.literal_column("id DESC")],
        unique=False,
    )


def downgrade() -> None:
    """仅在没有变更事实时移除本切片结构。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM change_records) THEN
                RAISE EXCEPTION '存在变更记录，拒绝执行有损降级';
            END IF;
        END
        $$
        """
    )
    op.drop_index("ix_change_records_service_occurred", table_name="change_records")
    op.drop_table("change_records")
