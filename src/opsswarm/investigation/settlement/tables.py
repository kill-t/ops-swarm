"""候选恢复结论与父运行的一次性持久收尾事实。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class CandidateSettlementRow(Base):
    __tablename__ = "investigation_candidate_settlements"
    __table_args__ = (
        ForeignKeyConstraint(
            ["investigation_run_id", "event_cursor"],
            ["agent_task_events.investigation_run_id", "agent_task_events.cursor"],
            name="fk_candidate_settlement_event",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "verification_version BETWEEN 1 AND 8 AND incident_version>=2 AND event_cursor>=1",
            name="ck_candidate_settlement_versions",
        ),
        CheckConstraint(
            "previous_run_status IN ('await_candidate','await_human')",
            name="ck_candidate_settlement_previous_status",
        ),
        CheckConstraint(
            "outcome IN ('recovered','not_recovered','source_failed','not_executed',"
            "'execution_unknown')",
            name="ck_candidate_settlement_outcome",
        ),
        CheckConstraint(
            "disposition IN ('recovery_verified','reinvestigation_required',"
            "'human_resolution_required')",
            name="ck_candidate_settlement_disposition",
        ),
        CheckConstraint(
            "execution_result_sha256 ~ '^[0-9a-f]{64}$' AND "
            "recovery_result_sha256 ~ '^[0-9a-f]{64}$' AND content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_candidate_settlement_hashes",
        ),
        CheckConstraint(
            "policy_version='candidate-settlement-v1'", name="ck_candidate_settlement_policy"
        ),
        CheckConstraint(
            "reason_code ~ '^[a-z][a-z0-9_]{0,99}$'", name="ck_candidate_settlement_reason"
        ),
    )
    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    recovery_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_recovery_jobs.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    candidate_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_candidate_evaluation_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    verification_version: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    execution_result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    recovery_result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    handoff_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_terminal_handoffs.id", ondelete="RESTRICT"), nullable=False
    )
    previous_run_status: Mapped[str] = mapped_column(String(16), nullable=False)
    previous_run_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    incident_version: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    disposition: Mapped[str] = mapped_column(String(40), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    event_cursor: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
