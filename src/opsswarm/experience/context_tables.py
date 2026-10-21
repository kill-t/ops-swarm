"""首轮协调实际使用的历史经验上下文快照映射。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class ExperienceContextRow(Base):
    """按运行唯一冻结的经验选择；首次调用只记录冻结来源。"""

    __tablename__ = "investigation_experience_contexts"
    __table_args__ = (
        UniqueConstraint("id", name="uq_investigation_experience_contexts_id"),
        UniqueConstraint("first_call_id", name="uq_investigation_experience_contexts_first_call"),
        CheckConstraint(
            "policy_version = 'coordination-experience-v1'",
            name="ck_investigation_experience_contexts_policy",
        ),
        CheckConstraint(
            "jsonb_typeof(content) = 'object' AND "
            "octet_length(content::text) <= 196608 AND "
            "COALESCE(content ->> 'id', '') = id::text AND "
            "COALESCE(content ->> 'policy_version', '') = policy_version AND "
            "COALESCE(content ->> 'search_policy_version', '') = 'experience-search-v2' AND "
            "COALESCE(jsonb_typeof(content -> 'scope'), '') = 'object' AND "
            "COALESCE(content #>> '{scope,run_id}', '') = investigation_run_id::text AND "
            "COALESCE(jsonb_typeof(content -> 'selected_at'), '') = 'string' AND "
            "COALESCE(jsonb_typeof(content -> 'items'), '') = 'array' AND "
            "jsonb_array_length(content -> 'items') <= 3",
            name="ck_investigation_experience_contexts_content",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_investigation_experience_contexts_hash",
        ),
        ForeignKeyConstraint(
            ["first_call_id", "investigation_run_id"],
            [
                "investigation_call_reservations.id",
                "investigation_call_reservations.investigation_run_id",
            ],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_investigation_experience_contexts_first_call",
        ),
    )

    investigation_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("investigation_runs.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    first_call_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
