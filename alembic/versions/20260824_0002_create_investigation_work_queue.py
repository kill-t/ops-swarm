"""创建调查运行与持久任务表。

版本标识：0002_investigation_queue
前置版本：0001_incident_storage
创建时间：2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_investigation_queue"
down_revision: str | None = "0001_incident_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立调查业务状态与 Worker 租约队列。"""

    op.create_table(
        "investigation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("graph_thread_id", sa.String(length=100), nullable=True),
        sa.Column("model_name", sa.String(length=200), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("mode IN ('single_agent')", name="ck_investigation_runs_mode"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_investigation_runs_status",
        ),
        sa.CheckConstraint(
            "result IS NULL OR jsonb_typeof(result) = 'object'",
            name="ck_investigation_runs_result_object",
        ),
        sa.CheckConstraint(
            "metrics IS NULL OR jsonb_typeof(metrics) = 'object'",
            name="ck_investigation_runs_metrics_object",
        ),
        sa.CheckConstraint(
            "last_error IS NULL OR jsonb_typeof(last_error) = 'object'",
            name="ck_investigation_runs_error_object",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND result IS NOT NULL) OR "
            "(status <> 'succeeded' AND result IS NULL)",
            name="ck_investigation_runs_result_shape",
        ),
        sa.CheckConstraint(
            "metrics IS NULL OR status = 'succeeded'",
            name="ck_investigation_runs_metrics_shape",
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR last_error IS NOT NULL",
            name="ck_investigation_runs_failure_shape",
        ),
        sa.CheckConstraint(
            "(status IN ('queued', 'running') AND completed_at IS NULL) OR "
            "(status IN ('succeeded', 'failed') AND completed_at IS NOT NULL)",
            name="ck_investigation_runs_completion_shape",
        ),
        sa.CheckConstraint(
            "status = 'queued' OR started_at IS NOT NULL",
            name="ck_investigation_runs_started_shape",
        ),
        sa.CheckConstraint(
            "created_at <= updated_at",
            name="ck_investigation_runs_time_order",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_investigation_runs_completed_time",
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("graph_thread_id"),
    )
    op.create_index(
        "uq_investigation_runs_active_incident",
        "investigation_runs",
        ["incident_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_investigation_runs_incident_created",
        "investigation_runs",
        ["incident_id", sa.literal_column("created_at DESC")],
        unique=False,
    )

    op.create_table(
        "work_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(length=100), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('run_single_agent_investigation')",
            name="ck_work_items_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_work_items_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1 AND attempt_count <= max_attempts",
            name="ck_work_items_attempts",
        ),
        sa.CheckConstraint(
            "last_error IS NULL OR jsonb_typeof(last_error) = 'object'",
            name="ck_work_items_error_object",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND locked_by IS NOT NULL AND locked_until IS NOT NULL "
            "AND lease_token IS NOT NULL) OR "
            "(status <> 'running' AND locked_by IS NULL AND locked_until IS NULL "
            "AND lease_token IS NULL)",
            name="ck_work_items_lease_shape",
        ),
        sa.CheckConstraint(
            "(status IN ('pending', 'running') AND completed_at IS NULL) OR "
            "(status IN ('completed', 'failed') AND completed_at IS NOT NULL)",
            name="ck_work_items_completion_shape",
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR last_error IS NOT NULL",
            name="ck_work_items_failure_shape",
        ),
        sa.CheckConstraint("created_at <= updated_at", name="ck_work_items_time_order"),
        sa.ForeignKeyConstraint(
            ["investigation_run_id"],
            ["investigation_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("investigation_run_id"),
    )
    op.create_index(
        "ix_work_items_pending_claim",
        "work_items",
        ["available_at", "created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_work_items_expired_lease",
        "work_items",
        ["locked_until"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    """按外键依赖逆序删除调查任务表。"""

    op.drop_index("ix_work_items_expired_lease", table_name="work_items")
    op.drop_index("ix_work_items_pending_claim", table_name="work_items")
    op.drop_table("work_items")
    op.drop_index("ix_investigation_runs_incident_created", table_name="investigation_runs")
    op.drop_index("uq_investigation_runs_active_incident", table_name="investigation_runs")
    op.drop_table("investigation_runs")
