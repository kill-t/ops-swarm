"""新增可信操作者的持久授权准入审计。"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0061_trusted_operator_identity"
down_revision: str | None = "0060_versioned_runbook_read"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "operator_authorizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("subject_id", sa.String(100), nullable=False),
        sa.Column("credential_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("permissions", postgresql.JSONB(), nullable=False),
        sa.Column("operation", sa.String(80), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("route", sa.String(256), nullable=False),
        sa.Column("resources", postgresql.JSONB(), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("policy_version = 'operator-auth-v1'", name="ck_operator_auth_policy"),
        sa.CheckConstraint("method = 'POST'", name="ck_operator_auth_method"),
        sa.CheckConstraint(
            "role IN ('viewer', 'engineer', 'approver', 'executor', 'alert_source')",
            name="ck_operator_auth_role",
        ),
        sa.CheckConstraint("body_sha256 ~ '^[0-9a-f]{64}$'", name="ck_operator_auth_body_hash"),
        sa.CheckConstraint(
            "jsonb_typeof(permissions) = 'array'", name="ck_operator_auth_permissions"
        ),
        sa.CheckConstraint("jsonb_typeof(resources) = 'object'", name="ck_operator_auth_resources"),
    )
    op.create_index(
        "ix_operator_auth_subject_time", "operator_authorizations", ["subject_id", "authorized_at"]
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM operator_authorizations) THEN
                RAISE EXCEPTION '不能删除已持久化的操作者授权记录';
            END IF;
        END $$;
    """)
    op.drop_index("ix_operator_auth_subject_time", table_name="operator_authorizations")
    op.drop_table("operator_authorizations")
