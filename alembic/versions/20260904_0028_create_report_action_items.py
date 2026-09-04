"""创建复盘改进行动项与追加式审计事件。

版本标识：0028_create_report_action_items
前置版本：0027_create_incident_reports
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0028_create_report_action_items"
down_revision: str | None = "0027_create_incident_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保存报告行动项当前状态及不可覆盖的状态历史。"""

    op.create_table(
        "report_action_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=False),
        sa.Column("owner_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_id", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_by_id", sa.String(length=100), nullable=True),
        sa.Column("completion_note", sa.String(length=500), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'completed')",
            name="ck_report_action_items_status",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_report_action_items_version_positive",
        ),
        sa.CheckConstraint(
            "due_at > created_at",
            name="ck_report_action_items_due_time",
        ),
        sa.CheckConstraint(
            "char_length(btrim(title)) BETWEEN 1 AND 200 AND "
            "char_length(btrim(description)) BETWEEN 1 AND 1000 AND "
            "char_length(btrim(owner_id)) BETWEEN 1 AND 100 AND "
            "char_length(btrim(created_by_id)) BETWEEN 1 AND 100 AND "
            "(completed_by_id IS NULL OR "
            "char_length(btrim(completed_by_id)) BETWEEN 1 AND 100) AND "
            "(completion_note IS NULL OR "
            "char_length(btrim(completion_note)) BETWEEN 1 AND 500)",
            name="ck_report_action_items_text",
        ),
        sa.CheckConstraint(
            "(status = 'open' AND version = 1 AND completed_by_id IS NULL "
            "AND completion_note IS NULL AND completed_at IS NULL) OR "
            "(status = 'completed' AND version = 2 AND completed_by_id IS NOT NULL "
            "AND completion_note IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_report_action_items_lifecycle",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_report_action_items_completion_time",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["incident_reports.id"],
            name="fk_report_action_items_report_id_incident_reports",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_action_items")),
        sa.UniqueConstraint(
            "report_id",
            "idempotency_key",
            name="uq_report_action_items_report_idempotency",
        ),
    )
    op.create_index(
        "ix_report_action_items_report_status_due",
        "report_action_items",
        ["report_id", "status", "due_at"],
    )

    op.create_table(
        "report_action_item_events",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("action_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("actor_id", sa.String(length=100), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("item_version", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('report_action_item.created', 'report_action_item.completed')",
            name="ck_report_action_item_events_type",
        ),
        sa.CheckConstraint(
            "(event_type = 'report_action_item.created' AND from_status IS NULL "
            "AND to_status = 'open' AND item_version = 1) OR "
            "(event_type = 'report_action_item.completed' AND from_status = 'open' "
            "AND to_status = 'completed' AND item_version = 2)",
            name="ck_report_action_item_events_shape",
        ),
        sa.CheckConstraint(
            "char_length(btrim(actor_id)) BETWEEN 1 AND 100 AND "
            "char_length(btrim(reason)) BETWEEN 1 AND 500",
            name="ck_report_action_item_events_text",
        ),
        sa.ForeignKeyConstraint(
            ["action_item_id"],
            ["report_action_items.id"],
            name="fk_report_action_item_events_action_item_id_report_action_items",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_action_item_events")),
        sa.UniqueConstraint(
            "action_item_id",
            "item_version",
            name="uq_report_action_item_events_version",
        ),
    )
    op.create_index(
        "ix_report_action_item_events_item_cursor",
        "report_action_item_events",
        ["action_item_id", "id"],
    )


def downgrade() -> None:
    """先删除审计事件，再删除报告行动项。"""

    op.drop_index(
        "ix_report_action_item_events_item_cursor",
        table_name="report_action_item_events",
    )
    op.drop_table("report_action_item_events")
    op.drop_index(
        "ix_report_action_items_report_status_due",
        table_name="report_action_items",
    )
    op.drop_table("report_action_items")
