"""创建评测实验身份、重复运行与模式级遥测。

版本标识：0012_evaluation_experiments
前置版本：0011_evaluation_storage
创建时间：2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012_evaluation_experiments"
down_revision: str | None = "0011_evaluation_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMPTY_TELEMETRY = """
{
  "input_tokens": 0,
  "output_tokens": 0,
  "total_tokens": 0,
  "estimated_cost_usd": "0",
  "failures": {
    "model_request_error": 0,
    "tool_error": 0,
    "timeout": 0,
    "structured_output_error": 0,
    "safety_rejection": 0
  }
}
"""


def upgrade() -> None:
    """固定可比实验配置，并为完成运行增加重复序号和遥测。"""

    op.create_table(
        "evaluation_experiments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("experiment_key", sa.String(length=200), nullable=False),
        sa.Column("evaluation_case_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evaluation_kind", sa.String(length=30), nullable=False),
        sa.Column("runner_name", sa.String(length=100), nullable=False),
        sa.Column("model_provider", sa.String(length=100), nullable=True),
        sa.Column("model_name", sa.String(length=200), nullable=True),
        sa.Column("model_parameters_hash", sa.String(length=64), nullable=True),
        sa.Column("single_agent_prompt_version", sa.String(length=100), nullable=False),
        sa.Column("multi_agent_prompt_version", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "evaluation_kind IN ('fixture_contract', 'real_model')",
            name="ck_evaluation_experiments_kind",
        ),
        sa.CheckConstraint(
            "(evaluation_kind = 'fixture_contract' "
            "AND model_provider IS NULL AND model_name IS NULL "
            "AND model_parameters_hash IS NULL) OR "
            "(evaluation_kind = 'real_model' "
            "AND model_provider IS NOT NULL AND model_name IS NOT NULL "
            "AND model_parameters_hash ~ '^[0-9a-f]{64}$')",
            name="ck_evaluation_experiments_model_identity",
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_case_set_id"],
            ["evaluation_cases.id"],
            name="fk_evaluation_experiments_case_set",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_key"),
    )

    op.add_column(
        "evaluation_runs",
        sa.Column("experiment_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "evaluation_runs",
        sa.Column("repetition_index", sa.Integer(), nullable=True),
    )
    op.add_column(
        "evaluation_runs",
        sa.Column(
            "single_agent_telemetry",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "evaluation_runs",
        sa.Column(
            "multi_agent_telemetry",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    op.execute(
        """
        INSERT INTO evaluation_experiments (
            id,
            experiment_key,
            evaluation_case_set_id,
            evaluation_kind,
            runner_name,
            model_provider,
            model_name,
            model_parameters_hash,
            single_agent_prompt_version,
            multi_agent_prompt_version,
            created_at
        )
        SELECT
            id,
            'fixture-backfill-' || sequence,
            evaluation_case_set_id,
            'fixture_contract',
            runner_name,
            NULL,
            NULL,
            NULL,
            'single-agent-v1',
            'multi-agent-parallel-v1',
            created_at
        FROM evaluation_runs
        """
    )
    telemetry_literal = _EMPTY_TELEMETRY.replace("'", "''")
    op.execute(
        sa.text(
            "UPDATE evaluation_runs SET "
            "experiment_id = id, repetition_index = 1, "
            f"single_agent_telemetry = '{telemetry_literal}'::jsonb, "
            f"multi_agent_telemetry = '{telemetry_literal}'::jsonb"
        )
    )

    op.alter_column("evaluation_runs", "experiment_id", nullable=False)
    op.alter_column("evaluation_runs", "repetition_index", nullable=False)
    op.alter_column("evaluation_runs", "single_agent_telemetry", nullable=False)
    op.alter_column("evaluation_runs", "multi_agent_telemetry", nullable=False)
    op.create_foreign_key(
        "fk_evaluation_runs_experiment",
        "evaluation_runs",
        "evaluation_experiments",
        ["experiment_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_evaluation_runs_repetition_index",
        "evaluation_runs",
        "repetition_index BETWEEN 1 AND 1000",
    )
    op.create_check_constraint(
        "ck_evaluation_runs_single_telemetry_object",
        "evaluation_runs",
        "jsonb_typeof(single_agent_telemetry) = 'object'",
    )
    op.create_check_constraint(
        "ck_evaluation_runs_multi_telemetry_object",
        "evaluation_runs",
        "jsonb_typeof(multi_agent_telemetry) = 'object'",
    )
    op.create_unique_constraint(
        "uq_evaluation_runs_experiment_repetition",
        "evaluation_runs",
        ["experiment_id", "repetition_index"],
    )


def downgrade() -> None:
    """存在评测审计时拒绝丢弃实验身份与遥测。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM evaluation_experiments) THEN
                RAISE EXCEPTION '存在评测实验审计记录，拒绝执行有损降级';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint(
        "uq_evaluation_runs_experiment_repetition",
        "evaluation_runs",
        type_="unique",
    )
    op.drop_constraint(
        "ck_evaluation_runs_multi_telemetry_object",
        "evaluation_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_evaluation_runs_single_telemetry_object",
        "evaluation_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_evaluation_runs_repetition_index",
        "evaluation_runs",
        type_="check",
    )
    op.drop_constraint(
        "fk_evaluation_runs_experiment",
        "evaluation_runs",
        type_="foreignkey",
    )
    op.drop_column("evaluation_runs", "multi_agent_telemetry")
    op.drop_column("evaluation_runs", "single_agent_telemetry")
    op.drop_column("evaluation_runs", "repetition_index")
    op.drop_column("evaluation_runs", "experiment_id")
    op.drop_table("evaluation_experiments")
