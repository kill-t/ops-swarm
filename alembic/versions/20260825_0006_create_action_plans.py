"""创建版本化处置计划与风险分级步骤。

版本标识：0006_action_plans
前置版本：0005_hypothesis_challenge
创建时间：2026-08-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_action_plans"
down_revision: str | None = "0005_hypothesis_challenge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立与假设版本绑定且尚未授权的处置计划草案。"""

    op.create_table(
        "action_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("hypothesis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.String(length=1000), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("requires_human_approval", sa.Boolean(), nullable=False),
        sa.Column("risk_policy_version", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_action_plans_version"),
        sa.CheckConstraint("status IN ('draft')", name="ck_action_plans_status"),
        sa.CheckConstraint(
            "risk_level IN ('low', 'medium', 'high', 'critical')",
            name="ck_action_plans_risk_level",
        ),
        sa.CheckConstraint(
            "risk_level NOT IN ('high', 'critical') OR requires_human_approval",
            name="ck_action_plans_high_risk_approval",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_run_id", "hypothesis_id"],
            ["hypotheses.investigation_run_id", "hypotheses.id"],
            name="fk_action_plans_same_run_hypothesis",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "hypothesis_id",
            "version",
            name="uq_action_plans_hypothesis_version",
        ),
    )
    op.create_index(
        "ix_action_plans_run_version",
        "action_plans",
        ["investigation_run_id", sa.literal_column("version DESC")],
        unique=False,
    )

    op.create_table(
        "action_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action_plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_name", sa.String(length=100), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("requires_human_approval", sa.Boolean(), nullable=False),
        sa.Column("risk_rule_code", sa.String(length=100), nullable=False),
        sa.Column("risk_rationale", sa.String(length=500), nullable=False),
        sa.Column("verification", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("step_order >= 1", name="ck_action_steps_order"),
        sa.CheckConstraint(
            "action_type IN ('rollback_release')",
            name="ck_action_steps_action_type",
        ),
        sa.CheckConstraint("target_type IN ('service')", name="ck_action_steps_target_type"),
        sa.CheckConstraint(
            "risk_level IN ('low', 'medium', 'high', 'critical')",
            name="ck_action_steps_risk_level",
        ),
        sa.CheckConstraint(
            "risk_level NOT IN ('high', 'critical') OR requires_human_approval",
            name="ck_action_steps_high_risk_approval",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(parameters) = 'object'",
            name="ck_action_steps_parameters_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(verification) = 'object'",
            name="ck_action_steps_verification_object",
        ),
        sa.ForeignKeyConstraint(
            ["action_plan_id"],
            ["action_plans.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "action_plan_id",
            "step_order",
            name="uq_action_steps_plan_order",
        ),
    )


def downgrade() -> None:
    """按依赖顺序删除处置步骤和计划版本。"""

    op.drop_table("action_steps")
    op.drop_index("ix_action_plans_run_version", table_name="action_plans")
    op.drop_table("action_plans")
