"""不可变专业结果与运行内有序进度事件的映射。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class AgentTaskResultRow(Base):
    __tablename__ = "agent_task_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["attempt_id", "task_id", "investigation_run_id"],
            [
                "agent_task_attempts.id",
                "agent_task_attempts.task_id",
                "agent_task_attempts.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_task_results_attempt",
        ),
        CheckConstraint(
            "jsonb_typeof(result) = 'object' AND octet_length(result::text) <= 262144",
            name="ck_task_results_size",
        ),
    )

    task_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentTaskEventRow(Base):
    __tablename__ = "agent_task_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "investigation_run_id"],
            ["agent_tasks.id", "agent_tasks.investigation_run_id"],
            ondelete="RESTRICT",
            name="fk_task_events_task",
        ),
        CheckConstraint("cursor >= 1", name="ck_task_events_cursor"),
        CheckConstraint(
            "event_type IN ('round.created', 'round.completed', 'round.failed', "
            "'task.started', 'task.succeeded', 'task.failed', 'task.interrupted', "
            "'task.evidence_saved', 'run.handed_off')",
            name="ck_task_events_type",
        ),
    )

    investigation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="RESTRICT"), primary_key=True
    )
    cursor: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
