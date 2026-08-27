"""创建版本化评测集、运行与逐场景结果。

版本标识：0011_evaluation_storage
前置版本：0010_verification_runs
创建时间：2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011_evaluation_storage"
down_revision: str | None = "0010_verification_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """保存不可变评测输入、聚合指标和逐场景对照。"""

    op.create_table(
        "evaluation_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_key", sa.String(length=120), nullable=False),
        sa.Column("dataset_content_hash", sa.String(length=64), nullable=False),
        sa.Column("scoring_policy_version", sa.String(length=100), nullable=False),
        sa.Column("scenario_count", sa.Integer(), nullable=False),
        sa.Column("dataset_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "dataset_content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_evaluation_cases_content_hash",
        ),
        sa.CheckConstraint(
            "scenario_count BETWEEN 1 AND 100",
            name="ck_evaluation_cases_scenario_count",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(dataset_snapshot) = 'object'",
            name="ck_evaluation_cases_snapshot_object",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset_content_hash"),
        sa.UniqueConstraint("dataset_key"),
    )
    op.create_table(
        "evaluation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "sequence",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("evaluation_case_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("report_content_hash", sa.String(length=64), nullable=False),
        sa.Column("runner_name", sa.String(length=100), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "single_agent_aggregate",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "multi_agent_aggregate",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("aggregate_delta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "report_content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_evaluation_runs_content_hash",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(single_agent_aggregate) = 'object'",
            name="ck_evaluation_runs_single_aggregate_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(multi_agent_aggregate) = 'object'",
            name="ck_evaluation_runs_multi_aggregate_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(aggregate_delta) = 'object'",
            name="ck_evaluation_runs_delta_object",
        ),
        sa.CheckConstraint(
            "generated_at <= created_at",
            name="ck_evaluation_runs_time_order",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_case_set_id"],
            ["evaluation_cases.id"],
            name="fk_evaluation_runs_case_set",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("sequence"),
    )
    op.create_index(
        "ix_evaluation_runs_sequence",
        "evaluation_runs",
        ["sequence"],
        unique=False,
    )
    op.create_table(
        "evaluation_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evaluation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_id", sa.String(length=100), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("comparison", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("position >= 1", name="ck_evaluation_results_position"),
        sa.CheckConstraint(
            "jsonb_typeof(comparison) = 'object'",
            name="ck_evaluation_results_comparison_object",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_run_id"],
            ["evaluation_runs.id"],
            name="fk_evaluation_results_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "evaluation_run_id",
            "position",
            name="uq_evaluation_results_run_position",
        ),
        sa.UniqueConstraint(
            "evaluation_run_id",
            "scenario_id",
            name="uq_evaluation_results_run_scenario",
        ),
    )


def downgrade() -> None:
    """仅在没有评测审计数据时移除本切片结构。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM evaluation_cases)
               OR EXISTS (SELECT 1 FROM evaluation_runs)
               OR EXISTS (SELECT 1 FROM evaluation_results) THEN
                RAISE EXCEPTION '存在评测审计记录，拒绝执行有损降级';
            END IF;
        END
        $$
        """
    )
    op.drop_table("evaluation_results")
    op.drop_index("ix_evaluation_runs_sequence", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
    op.drop_table("evaluation_cases")
