"""经验直接替代事实与命令结果的不可变持久映射。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class ExperienceReplacementRow(Base):
    """来源唯一、目标唯一的精确确认工件替代事实。"""

    __tablename__ = "experience_replacements"
    __table_args__ = (
        UniqueConstraint("entry_id", name="uq_experience_replacements_entry"),
        UniqueConstraint("target_entry_id", name="uq_experience_replacements_target"),
        UniqueConstraint("authorization_id", name="uq_experience_replacements_authorization"),
        ForeignKeyConstraint(
            ["entry_id", "entry_version", "entry_content_hash", "source_hash"],
            [
                "experience_entries.id",
                "experience_entries.version",
                "experience_entries.content_hash",
                "experience_entries.source_hash",
            ],
            ondelete="RESTRICT",
            name="fk_experience_replacements_entry_identity",
        ),
        ForeignKeyConstraint(
            [
                "target_entry_id",
                "target_entry_version",
                "target_content_hash",
                "target_source_hash",
            ],
            [
                "experience_entries.id",
                "experience_entries.version",
                "experience_entries.content_hash",
                "experience_entries.source_hash",
            ],
            ondelete="RESTRICT",
            name="fk_experience_replacements_target_identity",
        ),
        CheckConstraint(
            "entry_version = 1 AND target_entry_version = 1 AND state_version = 2",
            name="ck_experience_replacements_versions",
        ),
        CheckConstraint("entry_id <> target_entry_id", name="ck_experience_replacements_not_self"),
        CheckConstraint(
            "policy_version = 'experience-replacement-v1'", name="ck_experience_replacements_policy"
        ),
        CheckConstraint(
            "entry_content_hash ~ '^[0-9a-f]{64}$' AND source_hash ~ '^[0-9a-f]{64}$' "
            "AND target_content_hash ~ '^[0-9a-f]{64}$' "
            "AND target_source_hash ~ '^[0-9a-f]{64}$' AND command_hash ~ '^[0-9a-f]{64}$'",
            name="ck_experience_replacements_hashes",
        ),
        CheckConstraint(
            "char_length(btrim(replaced_by_id)) BETWEEN 1 AND 100 "
            "AND char_length(btrim(reason)) BETWEEN 1 AND 1000",
            name="ck_experience_replacements_text",
        ),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 8 AND 100 "
            "AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'",
            name="ck_experience_replacements_idempotency_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    entry_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    entry_version: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_entry_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    target_entry_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    replaced_by_id: Mapped[str] = mapped_column(String(100), nullable=False)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    replaced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExperienceReplacementCommandRow(Base):
    """每次授权关联独立业务结果，允许未知来源或目标的拒绝审计。"""

    __tablename__ = "experience_replacement_commands"
    __table_args__ = (
        UniqueConstraint(
            "authorization_id", name="uq_experience_replacement_commands_authorization"
        ),
        CheckConstraint(
            "jsonb_typeof(command) = 'object'", name="ck_experience_replacement_commands_command"
        ),
        CheckConstraint(
            "command_hash ~ '^[0-9a-f]{64}$'", name="ck_experience_replacement_commands_hash"
        ),
        CheckConstraint(
            "char_length(btrim(requested_by_id)) BETWEEN 1 AND 100",
            name="ck_experience_replacement_commands_subject",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,80}$'",
            name="ck_experience_replacement_commands_error_code",
        ),
        CheckConstraint(
            "(outcome = 'applied' AND replacement_id IS NOT NULL AND error_code IS NULL "
            "AND http_status = 201) OR "
            "(outcome = 'replayed' AND replacement_id IS NOT NULL AND error_code IS NULL "
            "AND http_status = 200) OR "
            "(outcome = 'error' AND error_code IS NOT NULL AND http_status IN (404, 409))",
            name="ck_experience_replacement_commands_outcome",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    entry_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    target_entry_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    requested_by_id: Mapped[str] = mapped_column(String(100), nullable=False)
    command: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    replacement_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("experience_replacements.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
