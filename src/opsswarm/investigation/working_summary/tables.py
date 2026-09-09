"""已完成专业任务的不可变工作摘要映射。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class AgentTaskWorkingSummaryRow(Base):
    """保存一任务一份、可追溯到成功尝试的工作摘要。"""

    __tablename__ = "agent_task_working_summaries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["attempt_id", "task_id", "investigation_run_id"],
            [
                "agent_task_attempts.id",
                "agent_task_attempts.task_id",
                "agent_task_attempts.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_task_working_summaries_attempt",
        ),
        CheckConstraint(
            "version = 'task-working-summary-v1'",
            name="ck_task_working_summaries_version",
        ),
        CheckConstraint(
            "jsonb_typeof(summary) = 'object' AND octet_length(summary::text) <= 262144",
            name="ck_task_working_summaries_size",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_task_working_summaries_hash",
        ),
    )

    task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("agent_task_results.task_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
