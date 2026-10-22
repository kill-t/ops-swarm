"""精确成功重评对应的唯一、不可变路由事实。"""

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


class HumanReviewRouteRow(Base):
    __tablename__ = "investigation_human_review_routes"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_human_review_routes_job"),
        ForeignKeyConstraint(
            ["job_id", "investigation_run_id"],
            [
                "investigation_human_review_jobs.id",
                "investigation_human_review_jobs.investigation_run_id",
            ],
            name="fk_human_review_routes_job_run",
            ondelete="RESTRICT",
        ),
        CheckConstraint("wait_version BETWEEN 2 AND 16", name="ck_human_review_routes_wait"),
        CheckConstraint(
            "preparation_source_sha256 ~ '^[0-9a-f]{64}$' AND "
            "result_sha256 ~ '^[0-9a-f]{64}$' AND content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_human_review_routes_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(decision)='object' AND octet_length(decision::text)<=1048576",
            name="ck_human_review_routes_content",
        ),
    )
    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_human_review_results.job_id", ondelete="RESTRICT"), nullable=False
    )
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    wait_version: Mapped[int] = mapped_column(Integer, nullable=False)
    preparation_source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_call_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_call_reservations.id", ondelete="RESTRICT"), nullable=False
    )
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
