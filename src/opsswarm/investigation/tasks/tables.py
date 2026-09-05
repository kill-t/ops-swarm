"""轮次、专业任务及尝试的 PostgreSQL 映射与完整性约束。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class InvestigationRoundRow(Base):
    """一轮不可变分派和可推进的轮次状态。"""

    __tablename__ = "investigation_rounds"
    __table_args__ = (
        UniqueConstraint("investigation_run_id", "round_number", name="uq_rounds_run_number"),
        UniqueConstraint("id", "investigation_run_id", name="uq_rounds_id_run"),
        CheckConstraint("round_number BETWEEN 1 AND 3", name="ck_rounds_number"),
        CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_rounds_status"),
        CheckConstraint(
            "(status = 'running') = (completed_at IS NULL)", name="ck_rounds_completion"
        ),
        CheckConstraint("completed_at >= created_at", name="ck_rounds_time"),
        CheckConstraint(
            "jsonb_typeof(specification) = 'object' AND octet_length(specification::text) <= 32768",
            name="ck_rounds_specification",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="RESTRICT"), nullable=False
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    goal: Mapped[str] = mapped_column(String(1000), nullable=False)
    specification: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentTaskRow(Base):
    """专业任务身份不随技术重试变化。"""

    __tablename__ = "agent_tasks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["round_id", "investigation_run_id"],
            ["investigation_rounds.id", "investigation_rounds.investigation_run_id"],
            ondelete="RESTRICT",
            name="fk_tasks_round_run",
        ),
        UniqueConstraint("round_id", "task_key", name="uq_tasks_round_key"),
        UniqueConstraint("id", "investigation_run_id", name="uq_tasks_id_run"),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')", name="ck_tasks_status"
        ),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 3 AND attempt_count BETWEEN 0 AND max_attempts",
            name="ck_tasks_attempts",
        ),
        CheckConstraint("version >= 1", name="ck_tasks_version"),
        CheckConstraint(
            "(status IN ('succeeded', 'failed')) = (completed_at IS NOT NULL)",
            name="ck_tasks_completion",
        ),
        CheckConstraint(
            "updated_at >= created_at AND completed_at >= created_at", name="ck_tasks_time"
        ),
        CheckConstraint(
            "jsonb_typeof(assignment) = 'object' AND octet_length(assignment::text) <= 8192",
            name="ck_tasks_assignment",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    round_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    task_key: Mapped[str] = mapped_column(String(64), nullable=False)
    assignment: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentTaskAttemptRow(Base):
    """关联父租约尝试并保留不可覆盖的专业执行历史。"""

    __tablename__ = "agent_task_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "investigation_run_id"],
            ["agent_tasks.id", "agent_tasks.investigation_run_id"],
            ondelete="RESTRICT",
            name="fk_task_attempts_task_run",
        ),
        ForeignKeyConstraint(
            ["investigation_attempt_id", "investigation_run_id"],
            ["investigation_attempts.id", "investigation_attempts.investigation_run_id"],
            ondelete="RESTRICT",
            name="fk_task_attempts_parent_run",
        ),
        UniqueConstraint("task_id", "attempt_number", name="uq_task_attempts_number"),
        UniqueConstraint("task_id", "start_version", name="uq_task_attempts_start_version"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'lease_expired')",
            name="ck_task_attempts_status",
        ),
        CheckConstraint(
            "attempt_number BETWEEN 1 AND 3 AND start_version >= 1",
            name="ck_task_attempts_numbers",
        ),
        CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name="ck_task_attempts_completion"
        ),
        CheckConstraint(
            "lease_expires_at > started_at AND finished_at >= started_at",
            name="ck_task_attempts_time",
        ),
        CheckConstraint(
            "(status IN ('running', 'lease_expired') AND outcome IS NULL) OR "
            "(status IN ('succeeded', 'failed') AND outcome IS NOT NULL AND "
            "jsonb_typeof(outcome) = 'object' AND outcome ? 'status' AND "
            "outcome->>'status' = status AND octet_length(outcome::text) <= 16384)",
            name="ck_task_attempts_outcome",
        ),
        Index(
            "uq_task_attempts_running",
            "task_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    investigation_attempt_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    start_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    outcome: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
