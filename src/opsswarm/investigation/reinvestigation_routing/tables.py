"""每个原运行唯一的不可变路由事实。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class ReflowRouteRow(Base):
    __tablename__ = "investigation_reflow_routes"
    __table_args__ = (
        CheckConstraint(
            "route IN ('follow_up','await_human','candidate_evaluation')",
            name="ck_reflow_routes_route",
        ),
        CheckConstraint(
            "jsonb_typeof(content)='object' AND octet_length(content::text)<=1048576 "
            "AND content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reflow_routes_content",
        ),
    )
    investigation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_reflow_review_results.job_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    route: Mapped[str] = mapped_column(String(24), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
