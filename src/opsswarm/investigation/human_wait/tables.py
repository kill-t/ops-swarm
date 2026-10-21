"""不可变人工事件与每次可信 HTTP 命令的业务结果审计。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class HumanWaitEventRow(Base):
    __tablename__ = "investigation_human_wait_events"
    __table_args__ = (
        UniqueConstraint(
            "investigation_run_id", "state_version", name="uq_human_wait_events_version"
        ),
        UniqueConstraint(
            "investigation_run_id", "subject_id", "idempotency_key", name="uq_human_wait_events_key"
        ),
        UniqueConstraint("authorization_id", name="uq_human_wait_events_authorization"),
        ForeignKeyConstraint(
            ["handoff_id", "investigation_run_id"],
            [
                "investigation_terminal_handoffs.id",
                "investigation_terminal_handoffs.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_human_wait_events_handoff",
        ),
        ForeignKeyConstraint(
            ["authorization_id"],
            ["investigation_human_wait_commands.authorization_id"],
            name="fk_human_wait_events_command",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("state_version BETWEEN 2 AND 17", name="ck_human_wait_events_version"),
        CheckConstraint("action IN ('supplement', 'cancel')", name="ck_human_wait_events_action"),
        CheckConstraint("command_sha256 ~ '^[0-9a-f]{64}$'", name="ck_human_wait_events_hash"),
        CheckConstraint(
            "char_length(btrim(subject_id)) BETWEEN 1 AND 100", name="ck_human_wait_events_subject"
        ),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 8 AND 100 AND "
            "idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'",
            name="ck_human_wait_events_key",
        ),
    )
    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    handoff_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(100), nullable=False)
    authorization_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    command_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HumanWaitCommandRow(Base):
    __tablename__ = "investigation_human_wait_commands"
    __table_args__ = (
        UniqueConstraint("authorization_id", name="uq_human_wait_commands_authorization"),
        CheckConstraint(
            "jsonb_typeof(command) = 'object' AND octet_length(command::text) <= 32768",
            name="ck_human_wait_commands_body",
        ),
        CheckConstraint(
            "octet_length(raw_body) BETWEEN 2 AND 65536", name="ck_human_wait_commands_raw"
        ),
        CheckConstraint("command_sha256 ~ '^[0-9a-f]{64}$'", name="ck_human_wait_commands_hash"),
        CheckConstraint(
            "char_length(btrim(subject_id)) BETWEEN 1 AND 100",
            name="ck_human_wait_commands_subject",
        ),
        CheckConstraint(
            "(outcome IN ('applied', 'replayed') AND event_id IS NOT NULL "
            "AND error_code IS NULL AND error_message IS NULL AND "
            "((outcome = 'applied' AND http_status=201) OR "
            "(outcome = 'replayed' AND http_status=200))) "
            "OR (outcome='error' AND event_id IS NULL AND error_code IS NOT NULL "
            "AND error_message IS NOT NULL AND http_status IN (404,409))",
            name="ck_human_wait_commands_outcome",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,80}$'",
            name="ck_human_wait_commands_error",
        ),
        Index("ix_human_wait_commands_run_cursor", "investigation_run_id", "cursor"),
    )
    cursor: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(100), nullable=False)
    command: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_body: Mapped[str] = mapped_column(Text, nullable=False)
    command_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("investigation_human_wait_events.id", ondelete="RESTRICT")
    )
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
