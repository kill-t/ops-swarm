"""专业任务首次取证材料的持久映射与归属约束。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class AgentTaskEvidenceRow(Base):
    __tablename__ = "agent_task_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["call_id", "task_id", "attempt_id", "investigation_run_id"],
            [
                "investigation_call_reservations.id",
                "investigation_call_reservations.task_id",
                "investigation_call_reservations.task_attempt_id",
                "investigation_call_reservations.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_task_evidence_call",
        ),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object' AND octet_length(snapshot::text) <= 262144",
            name="ck_task_evidence_size",
        ),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_task_evidence_hash"),
    )

    task_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    call_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
