"""可信经验审核决定、确认版本与命令结果的不可变持久映射。"""

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


class ExperienceReviewRow(Base):
    """精确候选版本唯一的可信工程师决定，不改变原候选。"""

    __tablename__ = "experience_reviews"
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_experience_reviews_candidate"),
        UniqueConstraint("authorization_id", name="uq_experience_reviews_authorization"),
        UniqueConstraint(
            "id",
            "candidate_id",
            "candidate_version",
            "candidate_content_hash",
            "source_hash",
            "decision",
            name="uq_experience_review_identity",
        ),
        ForeignKeyConstraint(
            ["candidate_id", "candidate_version", "candidate_content_hash", "source_hash"],
            [
                "experience_candidates.id",
                "experience_candidates.version",
                "experience_candidates.content_hash",
                "experience_candidates.source_hash",
            ],
            ondelete="RESTRICT",
            name="fk_experience_reviews_candidate_identity",
        ),
        CheckConstraint(
            "candidate_version >= 1 AND review_version = 1",
            name="ck_experience_reviews_versions",
        ),
        CheckConstraint(
            "policy_version = 'experience-review-v1'", name="ck_experience_reviews_policy"
        ),
        CheckConstraint("decision IN ('confirm', 'reject')", name="ck_experience_reviews_decision"),
        CheckConstraint(
            "candidate_content_hash ~ '^[0-9a-f]{64}$' AND source_hash ~ '^[0-9a-f]{64}$' "
            "AND command_hash ~ '^[0-9a-f]{64}$'",
            name="ck_experience_reviews_hashes",
        ),
        CheckConstraint(
            "char_length(btrim(reviewed_by_id)) BETWEEN 1 AND 100 "
            "AND char_length(btrim(reason)) BETWEEN 1 AND 1000",
            name="ck_experience_reviews_text",
        ),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 8 AND 100 "
            "AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'",
            name="ck_experience_reviews_idempotency_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    candidate_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    candidate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    review_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reviewed_by_id: Mapped[str] = mapped_column(String(100), nullable=False)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExperienceEntryRow(Base):
    """只能由精确确认决定产生的不可变经验版本。"""

    __tablename__ = "experience_entries"
    __table_args__ = (
        UniqueConstraint("review_id", name="uq_experience_entries_review"),
        UniqueConstraint("candidate_id", name="uq_experience_entries_candidate"),
        ForeignKeyConstraint(
            [
                "review_id",
                "candidate_id",
                "candidate_version",
                "candidate_content_hash",
                "source_hash",
                "review_decision",
            ],
            [
                "experience_reviews.id",
                "experience_reviews.candidate_id",
                "experience_reviews.candidate_version",
                "experience_reviews.candidate_content_hash",
                "experience_reviews.source_hash",
                "experience_reviews.decision",
            ],
            ondelete="RESTRICT",
            name="fk_experience_entries_confirmed_review",
        ),
        CheckConstraint(
            "candidate_version >= 1 AND version = 1 AND status = 'confirmed' "
            "AND review_decision = 'confirm'",
            name="ck_experience_entries_version_status",
        ),
        CheckConstraint(
            "candidate_content_hash ~ '^[0-9a-f]{64}$' AND source_hash ~ '^[0-9a-f]{64}$' "
            "AND content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_experience_entries_hashes",
        ),
        CheckConstraint("jsonb_typeof(content) = 'object'", name="ck_experience_entries_content"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    review_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    candidate_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    candidate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    review_decision: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExperienceReviewCommandRow(Base):
    """记录业务结果；未知候选也能保留有授权的 404 审计。"""

    __tablename__ = "experience_review_commands"
    __table_args__ = (
        UniqueConstraint("authorization_id", name="uq_experience_review_commands_authorization"),
        CheckConstraint(
            "jsonb_typeof(command) = 'object'", name="ck_experience_review_commands_command"
        ),
        CheckConstraint(
            "command_hash ~ '^[0-9a-f]{64}$'", name="ck_experience_review_commands_hash"
        ),
        CheckConstraint(
            "char_length(btrim(requested_by_id)) BETWEEN 1 AND 100",
            name="ck_experience_review_commands_subject",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,80}$'",
            name="ck_experience_review_commands_error_code",
        ),
        CheckConstraint(
            "(outcome = 'applied' AND review_id IS NOT NULL AND error_code IS NULL "
            "AND http_status = 201) OR "
            "(outcome = 'replayed' AND review_id IS NOT NULL AND error_code IS NULL "
            "AND http_status = 200) OR "
            "(outcome = 'error' AND error_code IS NOT NULL AND http_status IN (404, 409))",
            name="ck_experience_review_commands_outcome",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    requested_by_id: Mapped[str] = mapped_column(String(100), nullable=False)
    command: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    review_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("experience_reviews.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
