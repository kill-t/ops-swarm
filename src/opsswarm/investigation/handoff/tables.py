"""终端交接的不可变来源与当前父尝试映射。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class InvestigationTerminalHandoffRow(Base):
    __tablename__ = "investigation_terminal_handoffs"
    __table_args__ = (
        UniqueConstraint("investigation_run_id", name="uq_terminal_handoffs_run"),
        ForeignKeyConstraint(
            ["source_review_route_id", "investigation_run_id"],
            [
                "investigation_hypothesis_review_routes.id",
                "investigation_hypothesis_review_routes.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_terminal_handoffs_route",
        ),
        ForeignKeyConstraint(
            ["investigation_attempt_id", "investigation_run_id"],
            ["investigation_attempts.id", "investigation_attempts.investigation_run_id"],
            ondelete="RESTRICT",
            name="fk_terminal_handoffs_attempt",
        ),
        CheckConstraint("version = 'terminal-handoff-v1'", name="ck_terminal_handoffs_version"),
        CheckConstraint(
            "destination IN ('await_human', 'candidate_evaluation')",
            name="ck_terminal_handoffs_destination",
        ),
        CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'", name="ck_terminal_handoffs_hash"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    investigation_attempt_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_review_route_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    destination: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
