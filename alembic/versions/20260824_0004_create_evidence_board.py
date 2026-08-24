"""创建 Evidence Board 证据、专业报告及关联表。

版本标识：0004_evidence_board
前置版本：0003_multi_agent_worker
创建时间：2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_evidence_board"
down_revision: str | None = "0003_multi_agent_worker"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立可独立审计、不可跨运行引用的 Evidence Board。"""

    op.create_table(
        "evidence_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_key", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('log', 'metric', 'change')",
            name="ck_evidence_items_kind",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(attributes) = 'object'",
            name="ck_evidence_items_attributes_object",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_evidence_items_content_hash",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_run_id"],
            ["investigation_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "investigation_run_id",
            "evidence_key",
            name="uq_evidence_items_run_key",
        ),
        sa.UniqueConstraint(
            "investigation_run_id",
            "id",
            name="uq_evidence_items_run_id",
        ),
    )
    op.create_index(
        "ix_evidence_items_run_kind_observed",
        "evidence_items",
        ["investigation_run_id", "kind", sa.literal_column("observed_at DESC")],
        unique=False,
    )

    op.create_table(
        "specialist_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("specialist", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.String(length=1000), nullable=False),
        sa.Column("findings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "specialist IN ('log', 'metric', 'change')",
            name="ck_specialist_reports_specialist",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(findings) = 'array' AND jsonb_array_length(findings) BETWEEN 1 AND 20",
            name="ck_specialist_reports_findings_array",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_specialist_reports_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_run_id"],
            ["investigation_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "investigation_run_id",
            "specialist",
            name="uq_specialist_reports_run_specialist",
        ),
        sa.UniqueConstraint(
            "investigation_run_id",
            "id",
            name="uq_specialist_reports_run_id",
        ),
    )

    op.create_table(
        "specialist_report_evidence",
        sa.Column("investigation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("specialist_report_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["investigation_run_id", "evidence_item_id"],
            ["evidence_items.investigation_run_id", "evidence_items.id"],
            name="fk_report_evidence_same_run_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_run_id", "specialist_report_id"],
            ["specialist_reports.investigation_run_id", "specialist_reports.id"],
            name="fk_report_evidence_same_run_report",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "investigation_run_id",
            "specialist_report_id",
            "evidence_item_id",
        ),
    )


def downgrade() -> None:
    """按关联依赖逆序删除 Evidence Board。"""

    op.drop_table("specialist_report_evidence")
    op.drop_table("specialist_reports")
    op.drop_index("ix_evidence_items_run_kind_observed", table_name="evidence_items")
    op.drop_table("evidence_items")
