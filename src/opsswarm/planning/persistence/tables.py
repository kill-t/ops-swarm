"""版本化处置计划与动作步骤的 SQLAlchemy 映射。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base

RISK_LEVEL_SQL = "'low', 'medium', 'high', 'critical'"


class ActionPlanRow(Base):
    """一个与主根因假设绑定的不可变计划版本。"""

    __tablename__ = "action_plans"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_action_plans_version"),
        CheckConstraint(
            "status IN ('draft', 'awaiting_approval', 'approved', 'rejected', 'expired')",
            name="ck_action_plans_status",
        ),
        CheckConstraint(
            f"risk_level IN ({RISK_LEVEL_SQL})",
            name="ck_action_plans_risk_level",
        ),
        CheckConstraint(
            "risk_level NOT IN ('high', 'critical') OR requires_human_approval",
            name="ck_action_plans_high_risk_approval",
        ),
        ForeignKeyConstraint(
            ["investigation_run_id", "hypothesis_id"],
            ["hypotheses.investigation_run_id", "hypotheses.id"],
            ondelete="RESTRICT",
            name="fk_action_plans_same_run_hypothesis",
        ),
        UniqueConstraint(
            "hypothesis_id",
            "version",
            name="uq_action_plans_hypothesis_version",
        ),
        UniqueConstraint(
            "id",
            "version",
            name="uq_action_plans_id_version",
        ),
        Index(
            "ix_action_plans_run_version",
            "investigation_run_id",
            text("version DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    hypothesis_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(String(1000), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    requires_human_approval: Mapped[bool] = mapped_column(Boolean, nullable=False)
    risk_policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ActionStepRow(Base):
    """计划中的受限动作、参数、风险和恢复判据。"""

    __tablename__ = "action_steps"
    __table_args__ = (
        CheckConstraint("step_order >= 1", name="ck_action_steps_order"),
        CheckConstraint(
            "action_type IN ('rollback_release')",
            name="ck_action_steps_action_type",
        ),
        CheckConstraint("target_type IN ('service')", name="ck_action_steps_target_type"),
        CheckConstraint(
            f"risk_level IN ({RISK_LEVEL_SQL})",
            name="ck_action_steps_risk_level",
        ),
        CheckConstraint(
            "risk_level NOT IN ('high', 'critical') OR requires_human_approval",
            name="ck_action_steps_high_risk_approval",
        ),
        CheckConstraint(
            "jsonb_typeof(parameters) = 'object'",
            name="ck_action_steps_parameters_object",
        ),
        CheckConstraint(
            "jsonb_typeof(verification) = 'object'",
            name="ck_action_steps_verification_object",
        ),
        UniqueConstraint(
            "action_plan_id",
            "step_order",
            name="uq_action_steps_plan_order",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    action_plan_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("action_plans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_name: Mapped[str] = mapped_column(String(100), nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    requires_human_approval: Mapped[bool] = mapped_column(Boolean, nullable=False)
    risk_rule_code: Mapped[str] = mapped_column(String(100), nullable=False)
    risk_rationale: Mapped[str] = mapped_column(String(500), nullable=False)
    verification: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
