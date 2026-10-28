"""回流专业执行的独立投影、尝试与不可变材料。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class ExecutionWorkRow(Base):
    __tablename__ = "investigation_reflow_task_work"
    __table_args__ = (
        UniqueConstraint("task_id", "investigation_run_id", name="uq_reflow_task_work_id_run"),
        ForeignKeyConstraint(
            ["task_id", "investigation_run_id"],
            [
                "investigation_reflow_task_queue.id",
                "investigation_reflow_task_queue.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_reflow_task_work_queue",
        ),
        CheckConstraint(
            "policy_version='reinvestigation-task-v1' AND evidence_policy_version='reflo"
            "w-live-mcp-v1'",
            name="ck_reflow_task_work_policy",
        ),
        CheckConstraint(
            "dispatch_sha256 ~ '^[0-9a-f]{64}$' AND assignment_sha256 ~ '^[0-9a-f]{64}$'"
            " AND (effective_window IS NULL OR (jsonb_typeof(effective_window)='object' "
            "AND octet_length(effective_window::text)<=8192))",
            name="ck_reflow_task_work_material",
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','blocked','failed') AND max_attem"
            "pts BETWEEN 1 AND 3 AND attempt_count BETWEEN 0 AND max_attempts",
            name="ck_reflow_task_work_state",
        ),
        CheckConstraint(
            "(status='running' AND locked_by IS NOT NULL AND lease_token IS NOT NULL AND"
            " locked_until IS NOT NULL) OR (status<>'running' AND locked_by IS NULL AND "
            "lease_token IS NULL AND locked_until IS NULL)",
            name="ck_reflow_task_work_lease",
        ),
        CheckConstraint(
            "(status IN ('queued','running') AND completed_at IS NULL) OR (status IN ('s"
            "ucceeded','blocked','failed') AND completed_at IS NOT NULL)",
            name="ck_reflow_task_work_completion",
        ),
        CheckConstraint(
            "status NOT IN ('blocked','failed') OR last_error IS NOT NULL",
            name="ck_reflow_task_work_error",
        ),
        CheckConstraint(
            "created_at<=updated_at AND available_at>=created_at AND (completed_at IS NU"
            "LL OR completed_at>=created_at)",
            name="ck_reflow_task_work_time",
        ),
        Index("ix_reflow_task_work_claim", "status", "available_at", "created_at"),
    )
    task_id: Mapped[UUID] = mapped_column(primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(nullable=False)
    dispatch_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    assignment_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    evidence_policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    effective_window: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    lease_token: Mapped[UUID | None] = mapped_column()
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(100))


class ExecutionAttemptRow(Base):
    __tablename__ = "investigation_reflow_task_attempts"
    __table_args__ = (
        UniqueConstraint(
            "id", "task_id", "investigation_run_id", name="uq_reflow_task_attempts_scope"
        ),
        UniqueConstraint("task_id", "number", name="uq_reflow_task_attempts_number"),
        UniqueConstraint("lease_token", name="uq_reflow_task_attempts_token"),
        ForeignKeyConstraint(
            ["task_id", "investigation_run_id"],
            [
                "investigation_reflow_task_work.task_id",
                "investigation_reflow_task_work.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_reflow_task_attempts_work",
        ),
        CheckConstraint(
            "number BETWEEN 1 AND 3 AND status IN ('running','succeeded','failed','lease_expired')",
            name="ck_reflow_task_attempts_state",
        ),
        CheckConstraint(
            "(status='running' AND finished_at IS NULL AND error_code IS NULL) OR (statu"
            "s='succeeded' AND finished_at IS NOT NULL AND error_code IS NULL) OR (statu"
            "s IN ('failed','lease_expired') AND finished_at IS NOT NULL AND error_code "
            "IS NOT NULL)",
            name="ck_reflow_task_attempts_completion",
        ),
        CheckConstraint(
            "lease_expires_at>started_at AND (finished_at IS NULL OR finished_at>=started_at)",
            name="ck_reflow_task_attempts_time",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    task_id: Mapped[UUID] = mapped_column(nullable=False)
    investigation_run_id: Mapped[UUID] = mapped_column(nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(100), nullable=False)
    lease_token: Mapped[UUID] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))


class ExecutionEvidenceRow(Base):
    __tablename__ = "investigation_reflow_task_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["call_id", "task_id", "attempt_id", "investigation_run_id"],
            [
                "investigation_call_reservations.id",
                "investigation_call_reservations.reflow_task_id",
                "investigation_call_reservations.reflow_task_attempt_id",
                "investigation_call_reservations.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_reflow_task_evidence_call",
        ),
        CheckConstraint(
            "jsonb_typeof(snapshot)='object' AND octet_length(snapshot::text)<=262144 AN"
            "D snapshot_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reflow_task_evidence_content",
        ),
    )
    task_id: Mapped[UUID] = mapped_column(primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    call_id: Mapped[UUID] = mapped_column(nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionPacketRow(Base):
    __tablename__ = "investigation_reflow_task_packets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "investigation_run_id"],
            [
                "investigation_reflow_task_work.task_id",
                "investigation_reflow_task_work.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_reflow_task_packets_work",
        ),
        CheckConstraint(
            "jsonb_typeof(packet)='object' AND octet_length(packet::text)<=1048576 AND p"
            "acket_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reflow_task_packets_content",
        ),
    )
    task_id: Mapped[UUID] = mapped_column(primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(nullable=False)
    packet: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    packet_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExecutionResultRow(Base):
    __tablename__ = "investigation_reflow_task_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["attempt_id", "task_id", "investigation_run_id"],
            [
                "investigation_reflow_task_attempts.id",
                "investigation_reflow_task_attempts.task_id",
                "investigation_reflow_task_attempts.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_reflow_task_results_attempt",
        ),
        CheckConstraint(
            "jsonb_typeof(content)='object' AND octet_length(content::text)<=65536 AND c"
            "ontent_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reflow_task_results_content",
        ),
    )
    task_id: Mapped[UUID] = mapped_column(primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
