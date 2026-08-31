"""持久化调查数据来源模式。

版本标识：0020_investigation_data_mode
前置版本：0019_change_records
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020_investigation_data_mode"
down_revision: str | None = "0019_change_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """把历史运行明确标为冻结证据，并限制后续模式取值。"""

    op.add_column(
        "investigation_runs",
        sa.Column(
            "data_source_mode",
            sa.String(length=16),
            nullable=False,
            server_default="frozen",
        ),
    )
    op.create_check_constraint(
        "ck_investigation_runs_data_source_mode",
        "investigation_runs",
        "data_source_mode IN ('frozen', 'live')",
    )
    op.alter_column("investigation_runs", "data_source_mode", server_default=None)


def downgrade() -> None:
    """存在现场调查历史时拒绝丢失数据来源审计。"""

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM investigation_runs WHERE data_source_mode = 'live'
            ) THEN
                RAISE EXCEPTION '存在现场调查运行，拒绝丢失数据来源模式';
            END IF;
        END
        $$
        """
    )
    op.drop_constraint(
        "ck_investigation_runs_data_source_mode",
        "investigation_runs",
        type_="check",
    )
    op.drop_column("investigation_runs", "data_source_mode")
