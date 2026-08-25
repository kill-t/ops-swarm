"""创建根因假设及证据质疑关系。

版本标识：0005_hypothesis_challenge
前置版本：0004_evidence_board
创建时间：2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_hypothesis_challenge"
down_revision: str | None = "0004_evidence_board"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立主根因假设、质疑状态和同运行证据关系。"""

    op.create_table(
        "hypotheses",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_cause", sa.String(length=64), nullable=False),
        sa.Column("statement", sa.String(length=1000), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("challenge_summary", sa.String(length=1000), nullable=False),
        sa.Column("rule_version", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "root_cause IN ('release_500_regression', 'database_pool_misconfiguration', "
            "'redis_unavailable', 'undetermined')",
            name="ck_hypotheses_root_cause",
        ),
        sa.CheckConstraint(
            "status IN ('supported', 'contested', 'insufficient_evidence')",
            name="ck_hypotheses_status",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_hypotheses_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_run_id"],
            ["investigation_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("investigation_run_id", name="uq_hypotheses_run"),
        sa.UniqueConstraint(
            "investigation_run_id",
            "id",
            name="uq_hypotheses_run_id",
        ),
    )

    op.create_table(
        "hypothesis_evidence",
        sa.Column("investigation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("hypothesis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relation", sa.String(length=16), nullable=False),
        sa.Column("rule_code", sa.String(length=100), nullable=False),
        sa.Column("rationale", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "relation IN ('supports', 'refutes', 'context')",
            name="ck_hypothesis_evidence_relation",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_run_id", "evidence_item_id"],
            ["evidence_items.investigation_run_id", "evidence_items.id"],
            name="fk_hypothesis_evidence_same_run_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_run_id", "hypothesis_id"],
            ["hypotheses.investigation_run_id", "hypotheses.id"],
            name="fk_hypothesis_evidence_same_run_hypothesis",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "investigation_run_id",
            "hypothesis_id",
            "evidence_item_id",
        ),
    )


def downgrade() -> None:
    """按关联依赖逆序删除假设质疑模型。"""

    op.drop_table("hypothesis_evidence")
    op.drop_table("hypotheses")
