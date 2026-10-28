"""不可变的整轮回流分派与待执行专业任务。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class ReflowDispatchRow(Base):
    __tablename__ = "investigation_reflow_dispatches"
    __table_args__ = (
        CheckConstraint("status IN ('queued','blocked')", name="ck_reflow_dispatches_status"),
        CheckConstraint(
            "jsonb_typeof(content)='object' AND octet_length(content::text)<=1048576 "
            "AND content_sha256 ~ '^[0-9a-f]{64}$' "
            "AND source_result_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reflow_dispatches_content",
        ),
    )
    investigation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_reflow_results.job_id", ondelete="RESTRICT"), primary_key=True
    )
    source_result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReflowDispatchTaskRow(Base):
    __tablename__ = "investigation_reflow_task_queue"
    __table_args__ = (
        UniqueConstraint("investigation_run_id", "task_key", name="uq_reflow_queue_run_key"),
        UniqueConstraint("id", "investigation_run_id", name="uq_reflow_queue_id_run"),
        CheckConstraint("status='pending'", name="ck_reflow_queue_status"),
        CheckConstraint(
            "jsonb_typeof(assignment)='object' AND octet_length(assignment::text)<=8192 "
            "AND assignment_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reflow_queue_assignment",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_reflow_dispatches.investigation_run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    round_id: Mapped[UUID] = mapped_column(nullable=False)
    task_key: Mapped[str] = mapped_column(String(64), nullable=False)
    assignment: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    assignment_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
