"""调用预留账本的 PostgreSQL 映射与归属约束。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class CallReservationRow(Base):
    __tablename__ = "investigation_call_reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["investigation_attempt_id", "investigation_run_id"],
            ["investigation_attempts.id", "investigation_attempts.investigation_run_id"],
            ondelete="RESTRICT",
            name="fk_call_reservations_parent",
        ),
        ForeignKeyConstraint(
            ["task_attempt_id", "task_id", "investigation_run_id"],
            [
                "agent_task_attempts.id",
                "agent_task_attempts.task_id",
                "agent_task_attempts.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_call_reservations_task",
        ),
        UniqueConstraint("investigation_run_id", "sequence", name="uq_call_reservations_sequence"),
        UniqueConstraint(
            "investigation_run_id", "idempotency_key", name="uq_call_reservations_key"
        ),
        CheckConstraint("sequence BETWEEN 1 AND 160", name="ck_call_reservations_sequence"),
        CheckConstraint(
            "status IN ('reserved', 'succeeded', 'failed', 'unknown')",
            name="ck_call_reservations_status",
        ),
        CheckConstraint(
            "(status = 'reserved') = (finished_at IS NULL) AND finished_at >= reserved_at",
            name="ck_call_reservations_time",
        ),
        CheckConstraint(
            "(status IN ('failed', 'unknown')) = (error_code IS NOT NULL)",
            name="ck_call_reservations_error",
        ),
        CheckConstraint(
            "(operation IN ('query_logs', 'query_metrics', 'list_changes') AND kind = 'tool') OR "
            "(operation IN ('analyze_log', 'analyze_metric', 'analyze_change', 'synthesize') "
            "AND kind = 'model')",
            name="ck_call_reservations_operation",
        ),
        CheckConstraint(
            "(operation = 'synthesize' AND task_id IS NULL AND task_attempt_id IS NULL) OR "
            "(operation <> 'synthesize' AND task_id IS NOT NULL AND task_attempt_id IS NOT NULL)",
            name="ck_call_reservations_scope",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    investigation_attempt_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    task_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    task_attempt_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
