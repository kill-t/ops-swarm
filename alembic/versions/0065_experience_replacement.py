"""新增精确经验替代事实、结果审计及原生终态互斥守卫。"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0065_experience_replacement"
down_revision: str | None = "0064_experience_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "experience_replacements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entry_version", sa.Integer(), nullable=False),
        sa.Column("entry_content_hash", sa.String(64), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("target_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_entry_version", sa.Integer(), nullable=False),
        sa.Column("target_content_hash", sa.String(64), nullable=False),
        sa.Column("target_source_hash", sa.String(64), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.String(40), nullable=False),
        sa.Column("replaced_by_id", sa.String(100), nullable=False),
        sa.Column("authorization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("replaced_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("entry_id", name="uq_experience_replacements_entry"),
        sa.UniqueConstraint("target_entry_id", name="uq_experience_replacements_target"),
        sa.UniqueConstraint("authorization_id", name="uq_experience_replacements_authorization"),
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
            name="fk_experience_replacements_entry_identity",
        ),
        sa.ForeignKeyConstraint(
            [
                "target_entry_id",
                "target_entry_version",
                "target_content_hash",
                "target_source_hash",
            ],
            [
                "experience_entries.id",
                "experience_entries.version",
                "experience_entries.content_hash",
                "experience_entries.source_hash",
            ],
            ondelete="RESTRICT",
            name="fk_experience_replacements_target_identity",
        ),
        sa.CheckConstraint(
            "entry_version = 1 AND target_entry_version = 1 AND state_version = 2",
            name="ck_experience_replacements_versions",
        ),
        sa.CheckConstraint(
            "entry_id <> target_entry_id", name="ck_experience_replacements_not_self"
        ),
        sa.CheckConstraint(
            "policy_version = 'experience-replacement-v1'", name="ck_experience_replacements_policy"
        ),
        sa.CheckConstraint(
            "entry_content_hash ~ '^[0-9a-f]{64}$' AND source_hash ~ '^[0-9a-f]{64}$' "
            "AND target_content_hash ~ '^[0-9a-f]{64}$' "
            "AND target_source_hash ~ '^[0-9a-f]{64}$' AND command_hash ~ '^[0-9a-f]{64}$'",
            name="ck_experience_replacements_hashes",
        ),
        sa.CheckConstraint(
            "char_length(btrim(replaced_by_id)) BETWEEN 1 AND 100 "
            "AND char_length(btrim(reason)) BETWEEN 1 AND 1000",
            name="ck_experience_replacements_text",
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) BETWEEN 8 AND 100 "
            "AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'",
            name="ck_experience_replacements_idempotency_key",
        ),
    )
    op.create_table(
        "experience_replacement_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("authorization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by_id", sa.String(100), nullable=False),
        sa.Column("command", postgresql.JSONB(), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("replacement_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "authorization_id", name="uq_experience_replacement_commands_authorization"
        ),
        sa.ForeignKeyConstraint(
            ["authorization_id"], ["operator_authorizations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["replacement_id"], ["experience_replacements.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(command) = 'object'", name="ck_experience_replacement_commands_command"
        ),
        sa.CheckConstraint(
            "command_hash ~ '^[0-9a-f]{64}$'", name="ck_experience_replacement_commands_hash"
        ),
        sa.CheckConstraint(
            "char_length(btrim(requested_by_id)) BETWEEN 1 AND 100",
            name="ck_experience_replacement_commands_subject",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,80}$'",
            name="ck_experience_replacement_commands_error_code",
        ),
        sa.CheckConstraint(
            "(outcome = 'applied' AND replacement_id IS NOT NULL AND error_code IS NULL "
            "AND http_status = 201) OR "
            "(outcome = 'replayed' AND replacement_id IS NOT NULL AND error_code IS NULL "
            "AND http_status = 200) OR "
            "(outcome = 'error' AND error_code IS NOT NULL AND http_status IN (404, 409))",
            name="ck_experience_replacement_commands_outcome",
        ),
    )
    op.execute("""
        CREATE FUNCTION guard_experience_replacement_insert() RETURNS trigger AS $$
        DECLARE
            source_entry experience_entries%ROWTYPE;
            target_entry experience_entries%ROWTYPE;
            scope_key text;
            source_scope text;
            target_scope text;
        BEGIN
            IF NEW.entry_id = NEW.target_entry_id THEN
                RETURN NEW;
            END IF;
            PERFORM id FROM experience_entries
                WHERE id IN (NEW.entry_id, NEW.target_entry_id)
                ORDER BY id FOR UPDATE;
            SELECT * INTO source_entry FROM experience_entries WHERE id = NEW.entry_id;
            SELECT * INTO target_entry FROM experience_entries WHERE id = NEW.target_entry_id;
            IF source_entry.id IS NULL OR target_entry.id IS NULL THEN
                RETURN NEW;
            END IF;
            IF EXISTS (
                SELECT 1 FROM experience_disablements
                WHERE entry_id IN (NEW.entry_id, NEW.target_entry_id)
            ) THEN
                RAISE EXCEPTION '已停用经验不能参与首次替代';
            END IF;
            IF EXISTS (
                SELECT 1 FROM experience_replacements
                WHERE entry_id IN (NEW.entry_id, NEW.target_entry_id)
            ) THEN
                RAISE EXCEPTION '已替代来源不能参与新的替代';
            END IF;
            IF target_entry.created_at <= source_entry.created_at THEN
                RAISE EXCEPTION '替代目标确认时间必须严格晚于来源';
            END IF;
            FOREACH scope_key IN ARRAY ARRAY['service_name', 'environment', 'data_source_mode'] LOOP
                source_scope := source_entry.content -> 'candidate_snapshot' ->> scope_key;
                target_scope := target_entry.content -> 'candidate_snapshot' ->> scope_key;
                IF source_scope IS NULL OR target_scope IS NULL
                   OR btrim(source_scope) = '' OR btrim(target_scope) = ''
                   OR source_scope IS DISTINCT FROM target_scope THEN
                    RAISE EXCEPTION '替代双方服务环境和来源范围必须非空一致';
                END IF;
            END LOOP;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql VOLATILE;
        CREATE TRIGGER experience_replacement_insert_guard
            BEFORE INSERT ON experience_replacements
            FOR EACH ROW EXECUTE FUNCTION guard_experience_replacement_insert();

        CREATE FUNCTION guard_experience_disablement_insert() RETURNS trigger AS $$
        BEGIN
            PERFORM id FROM experience_entries WHERE id = NEW.entry_id FOR UPDATE;
            IF EXISTS (SELECT 1 FROM experience_replacements WHERE entry_id = NEW.entry_id) THEN
                RAISE EXCEPTION '已替代来源不能再停用';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql VOLATILE;
        CREATE TRIGGER experience_disablement_terminal_guard
            BEFORE INSERT ON experience_disablements
            FOR EACH ROW EXECUTE FUNCTION guard_experience_disablement_insert();

        CREATE FUNCTION protect_experience_replacement_fact() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '经验替代事实和业务结果不可修改或删除';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER experience_replacement_immutable
            BEFORE UPDATE OR DELETE ON experience_replacements
            FOR EACH ROW EXECUTE FUNCTION protect_experience_replacement_fact();
        CREATE TRIGGER experience_replacement_command_immutable
            BEFORE UPDATE OR DELETE ON experience_replacement_commands
            FOR EACH ROW EXECUTE FUNCTION protect_experience_replacement_fact();
    """)


def downgrade() -> None:
    op.execute("""
        LOCK TABLE experience_replacement_commands, experience_replacements,
                   experience_disablements IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM experience_replacements)
               OR EXISTS (SELECT 1 FROM experience_replacement_commands) THEN
                RAISE EXCEPTION '不能删除已持久化的经验替代或结果事实';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER experience_disablement_terminal_guard ON experience_disablements")
    op.drop_table("experience_replacement_commands")
    op.drop_table("experience_replacements")
    op.execute("DROP FUNCTION guard_experience_disablement_insert()")
    op.execute("DROP FUNCTION guard_experience_replacement_insert()")
    op.execute("DROP FUNCTION protect_experience_replacement_fact()")
