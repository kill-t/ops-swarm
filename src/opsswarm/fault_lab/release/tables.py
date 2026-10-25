"""单服务当前状态及不可变发布演练命令映射。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class LabReleaseStateRow(Base):
    __tablename__ = "lab_release_states"
    __table_args__ = (
        CheckConstraint("service_name='order-service'", name="ck_lab_release_state_service"),
        CheckConstraint(
            "version IN ('stable','2026.01.01-rc1')", name="ck_lab_release_state_version"
        ),
        CheckConstraint("revision>=1 AND command_count>=0", name="ck_lab_release_state_counters"),
        CheckConstraint(
            "(command_count=0) = (last_command_id IS NULL)", name="ck_lab_release_state_head"
        ),
    )
    service_name: Mapped[str] = mapped_column(String(50), primary_key=True)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    command_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_command_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "lab_release_commands.id",
            name="fk_lab_release_state_command",
            use_alter=True,
            ondelete="RESTRICT",
        )
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LabReleaseCommandRow(Base):
    __tablename__ = "lab_release_commands"
    __table_args__ = (
        UniqueConstraint("service_name", "sequence", name="uq_lab_release_command_sequence"),
        UniqueConstraint("service_name", "idempotency_key", name="uq_lab_release_command_key"),
        CheckConstraint(
            "kind IN ('cas','legacy_fault_switch')", name="ck_lab_release_command_kind"
        ),
        CheckConstraint(
            "sequence>=1 AND previous_revision>=1 AND revision>=1",
            name="ck_lab_release_command_revision",
        ),
        CheckConstraint(
            "previous_version IN ('stable','2026.01.01-rc1') AND "
            "version IN ('stable','2026.01.01-rc1')",
            name="ck_lab_release_command_version",
        ),
        CheckConstraint("command_sha256 ~ '^[0-9a-f]{64}$'", name="ck_lab_release_command_hash"),
        CheckConstraint(
            "jsonb_typeof(command)='object' AND octet_length(command::text)<=4096",
            name="ck_lab_release_command_body",
        ),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 8 AND 100 AND "
            "idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'",
            name="ck_lab_release_command_key",
        ),
    )
    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    service_name: Mapped[str] = mapped_column(
        ForeignKey("lab_release_states.service_name", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    command: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    command_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_version: Mapped[str] = mapped_column(String(40), nullable=False)
    previous_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
