"""固定窗口与不可变CAS原文的远端操作映射。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class LabReleaseDispatchRow(Base):
    __tablename__ = "lab_release_dispatches"
    __table_args__ = (
        UniqueConstraint("command_id", name="uq_lab_release_dispatch_command"),
        CheckConstraint("policy_version='lab-release-dispatch-v1'", name="ck_lab_dispatch_policy"),
        CheckConstraint("command_sha256 ~ '^[0-9a-f]{64}$'", name="ck_lab_dispatch_hash"),
        CheckConstraint(
            "jsonb_typeof(command)='object' AND octet_length(command::text)<=4096",
            name="ck_lab_dispatch_body",
        ),
        CheckConstraint(
            "not_before<=created_at AND created_at<expires_at AND "
            "expires_at-not_before<=interval '30 seconds'",
            name="ck_lab_dispatch_window",
        ),
        CheckConstraint(
            "(status='prepared' AND command_id IS NULL AND finished_at IS NULL) OR "
            "(status='applied' AND command_id IS NOT NULL AND finished_at>=created_at "
            "AND finished_at<expires_at) OR "
            "(status='cancelled' AND command_id IS NULL AND finished_at>=created_at)",
            name="ck_lab_dispatch_status",
        ),
    )
    operation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    command: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    command_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    command_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("lab_release_commands.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
