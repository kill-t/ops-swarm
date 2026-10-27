"""候选收尾驱动的不可变 Incident 收敛事实。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class CandidateIncidentConvergenceRow(Base):
    __tablename__ = "candidate_incident_convergences"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(content)='object' AND octet_length(content::text)<=16384 AND "
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_candidate_convergence_content",
        ),
    )

    investigation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_candidate_settlements.investigation_run_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="RESTRICT"), nullable=False
    )
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
