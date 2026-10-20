"""新增不可变经验停用事实、业务结果及有界检索范围索引。"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0064_experience_lifecycle"
down_revision: str | None = "0063_trusted_experience_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_experience_entry_identity",
        "experience_entries",
        ["id", "version", "content_hash", "source_hash"],
    )
    op.create_index(
        "ix_experience_entries_scope",
        "experience_entries",
        [
            sa.text("((content -> 'candidate_snapshot'::text) ->> 'service_name'::text)"),
            sa.text("((content -> 'candidate_snapshot'::text) ->> 'environment'::text)"),
            sa.text("((content -> 'candidate_snapshot'::text) ->> 'data_source_mode'::text)"),
            sa.text("created_at DESC"),
            "id",
        ],
    )
    op.create_table(
        "experience_disablements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entry_version", sa.Integer(), nullable=False),
        sa.Column("entry_content_hash", sa.String(64), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.String(40), nullable=False),
        sa.Column("disabled_by_id", sa.String(100), nullable=False),
        sa.Column("authorization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("entry_id", name="uq_experience_disablements_entry"),
        sa.UniqueConstraint("authorization_id", name="uq_experience_disablements_authorization"),
        sa.ForeignKeyConstraint(
            ["authorization_id"], ["operator_authorizations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["entry_id", "entry_version", "entry_content_hash", "source_hash"],
            [
                "experience_entries.id",
                "experience_entries.version",
                "experience_entries.content_hash",
                "experience_entries.source_hash",
            ],
            ondelete="RESTRICT",
            name="fk_experience_disablements_entry_identity",
        ),
        sa.CheckConstraint(
            "entry_version = 1 AND state_version = 2", name="ck_experience_disablements_versions"
        ),
        sa.CheckConstraint(
            "policy_version = 'experience-lifecycle-v1'", name="ck_experience_disablements_policy"
        ),
        sa.CheckConstraint(
            "entry_content_hash ~ '^[0-9a-f]{64}$' AND source_hash ~ '^[0-9a-f]{64}$' "
            "AND command_hash ~ '^[0-9a-f]{64}$'",
            name="ck_experience_disablements_hashes",
        ),
        sa.CheckConstraint(
            "char_length(btrim(disabled_by_id)) BETWEEN 1 AND 100 "
            "AND char_length(btrim(reason)) BETWEEN 1 AND 1000",
            name="ck_experience_disablements_text",
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) BETWEEN 8 AND 100 "
            "AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'",
            name="ck_experience_disablements_idempotency_key",
        ),
    )
    op.create_table(
        "experience_lifecycle_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("authorization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by_id", sa.String(100), nullable=False),
        sa.Column("command", postgresql.JSONB(), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("disablement_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "authorization_id", name="uq_experience_lifecycle_commands_authorization"
        ),
        sa.ForeignKeyConstraint(
            ["authorization_id"], ["operator_authorizations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["disablement_id"], ["experience_disablements.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(command) = 'object'", name="ck_experience_lifecycle_commands_command"
        ),
        sa.CheckConstraint(
            "command_hash ~ '^[0-9a-f]{64}$'", name="ck_experience_lifecycle_commands_hash"
        ),
        sa.CheckConstraint(
            "char_length(btrim(requested_by_id)) BETWEEN 1 AND 100",
            name="ck_experience_lifecycle_commands_subject",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,80}$'",
            name="ck_experience_lifecycle_commands_error_code",
        ),
        sa.CheckConstraint(
            "(outcome = 'applied' AND disablement_id IS NOT NULL AND error_code IS NULL "
            "AND http_status = 201) OR "
            "(outcome = 'replayed' AND disablement_id IS NOT NULL AND error_code IS NULL "
            "AND http_status = 200) OR "
            "(outcome = 'error' AND error_code IS NOT NULL AND http_status IN (404, 409))",
            name="ck_experience_lifecycle_commands_outcome",
        ),
    )
    op.execute("""
        CREATE FUNCTION protect_experience_lifecycle_fact() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '经验停用事实和生命周期业务结果不可修改或删除';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER experience_disablement_immutable
            BEFORE UPDATE OR DELETE ON experience_disablements
            FOR EACH ROW EXECUTE FUNCTION protect_experience_lifecycle_fact();
        CREATE TRIGGER experience_lifecycle_command_immutable
            BEFORE UPDATE OR DELETE ON experience_lifecycle_commands
            FOR EACH ROW EXECUTE FUNCTION protect_experience_lifecycle_fact();
    """)


def downgrade() -> None:
    # 检查与 DDL 持有同一排他锁，拒绝丢失检查后提交的新治理事实。
    op.execute("""
        LOCK TABLE experience_lifecycle_commands, experience_disablements
            IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM experience_disablements)
               OR EXISTS (SELECT 1 FROM experience_lifecycle_commands) THEN
                RAISE EXCEPTION '不能删除已持久化的经验停用或生命周期事实';
            END IF;
        END $$;
    """)
    op.drop_table("experience_lifecycle_commands")
    op.drop_table("experience_disablements")
    op.execute("DROP FUNCTION protect_experience_lifecycle_fact()")
    op.drop_index("ix_experience_entries_scope", table_name="experience_entries")
    op.drop_constraint("uq_experience_entry_identity", "experience_entries", type_="unique")
