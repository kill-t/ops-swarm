"""新增可信经验审核、确认版本与独立业务结果审计。"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0063_trusted_experience_review"
down_revision: str | None = "0062_experience_candidates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_experience_candidate_identity",
        "experience_candidates",
        ["id", "version", "content_hash", "source_hash"],
    )
    op.create_table(
        "experience_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_version", sa.Integer(), nullable=False),
        sa.Column("candidate_content_hash", sa.String(64), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("review_version", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.String(40), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reviewed_by_id", sa.String(100), nullable=False),
        sa.Column("authorization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("candidate_id", name="uq_experience_reviews_candidate"),
        sa.UniqueConstraint("authorization_id", name="uq_experience_reviews_authorization"),
        sa.UniqueConstraint(
            "id",
            "candidate_id",
            "candidate_version",
            "candidate_content_hash",
            "source_hash",
            "decision",
            name="uq_experience_review_identity",
        ),
        sa.ForeignKeyConstraint(
            ["authorization_id"], ["operator_authorizations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id", "candidate_version", "candidate_content_hash", "source_hash"],
            [
                "experience_candidates.id",
                "experience_candidates.version",
                "experience_candidates.content_hash",
                "experience_candidates.source_hash",
            ],
            ondelete="RESTRICT",
            name="fk_experience_reviews_candidate_identity",
        ),
        sa.CheckConstraint(
            "candidate_version >= 1 AND review_version = 1",
            name="ck_experience_reviews_versions",
        ),
        sa.CheckConstraint(
            "policy_version = 'experience-review-v1'", name="ck_experience_reviews_policy"
        ),
        sa.CheckConstraint(
            "decision IN ('confirm', 'reject')", name="ck_experience_reviews_decision"
        ),
        sa.CheckConstraint(
            "candidate_content_hash ~ '^[0-9a-f]{64}$' AND source_hash ~ '^[0-9a-f]{64}$' "
            "AND command_hash ~ '^[0-9a-f]{64}$'",
            name="ck_experience_reviews_hashes",
        ),
        sa.CheckConstraint(
            "char_length(btrim(reviewed_by_id)) BETWEEN 1 AND 100 "
            "AND char_length(btrim(reason)) BETWEEN 1 AND 1000",
            name="ck_experience_reviews_text",
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) BETWEEN 8 AND 100 "
            "AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'",
            name="ck_experience_reviews_idempotency_key",
        ),
    )
    op.create_table(
        "experience_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_version", sa.Integer(), nullable=False),
        sa.Column("candidate_content_hash", sa.String(64), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("review_decision", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("review_id", name="uq_experience_entries_review"),
        sa.UniqueConstraint("candidate_id", name="uq_experience_entries_candidate"),
        sa.ForeignKeyConstraint(
            [
                "review_id",
                "candidate_id",
                "candidate_version",
                "candidate_content_hash",
                "source_hash",
                "review_decision",
            ],
            [
                "experience_reviews.id",
                "experience_reviews.candidate_id",
                "experience_reviews.candidate_version",
                "experience_reviews.candidate_content_hash",
                "experience_reviews.source_hash",
                "experience_reviews.decision",
            ],
            ondelete="RESTRICT",
            name="fk_experience_entries_confirmed_review",
        ),
        sa.CheckConstraint(
            "candidate_version >= 1 AND version = 1 AND status = 'confirmed' "
            "AND review_decision = 'confirm'",
            name="ck_experience_entries_version_status",
        ),
        sa.CheckConstraint(
            "candidate_content_hash ~ '^[0-9a-f]{64}$' AND source_hash ~ '^[0-9a-f]{64}$' "
            "AND content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_experience_entries_hashes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object'", name="ck_experience_entries_content"
        ),
    )
    op.create_table(
        "experience_review_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("authorization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by_id", sa.String(100), nullable=False),
        sa.Column("command", postgresql.JSONB(), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("review_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("authorization_id", name="uq_experience_review_commands_authorization"),
        sa.ForeignKeyConstraint(
            ["authorization_id"], ["operator_authorizations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["review_id"], ["experience_reviews.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "jsonb_typeof(command) = 'object'", name="ck_experience_review_commands_command"
        ),
        sa.CheckConstraint(
            "command_hash ~ '^[0-9a-f]{64}$'", name="ck_experience_review_commands_hash"
        ),
        sa.CheckConstraint(
            "char_length(btrim(requested_by_id)) BETWEEN 1 AND 100",
            name="ck_experience_review_commands_subject",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,80}$'",
            name="ck_experience_review_commands_error_code",
        ),
        sa.CheckConstraint(
            "(outcome = 'applied' AND review_id IS NOT NULL AND error_code IS NULL "
            "AND http_status = 201) OR "
            "(outcome = 'replayed' AND review_id IS NOT NULL AND error_code IS NULL "
            "AND http_status = 200) OR "
            "(outcome = 'error' AND error_code IS NOT NULL AND http_status IN (404, 409))",
            name="ck_experience_review_commands_outcome",
        ),
    )
    op.execute("""
        CREATE FUNCTION protect_experience_review_fact() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '经验审核决定、确认版本和业务结果不可修改或删除';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER experience_review_immutable
            BEFORE UPDATE OR DELETE ON experience_reviews
            FOR EACH ROW EXECUTE FUNCTION protect_experience_review_fact();
        CREATE TRIGGER experience_entry_immutable
            BEFORE UPDATE OR DELETE ON experience_entries
            FOR EACH ROW EXECUTE FUNCTION protect_experience_review_fact();
        CREATE TRIGGER experience_review_command_immutable
            BEFORE UPDATE OR DELETE ON experience_review_commands
            FOR EACH ROW EXECUTE FUNCTION protect_experience_review_fact();
    """)


def downgrade() -> None:
    # 先阻止并发写入，再检查空表，避免检查与删除之间丢失新事实。
    op.execute("""
        LOCK TABLE experience_review_commands, experience_entries, experience_reviews
            IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM experience_reviews)
               OR EXISTS (SELECT 1 FROM experience_entries)
               OR EXISTS (SELECT 1 FROM experience_review_commands) THEN
                RAISE EXCEPTION '不能删除已持久化的经验审核或确认事实';
            END IF;
        END $$;
    """)
    op.drop_table("experience_review_commands")
    op.drop_table("experience_entries")
    op.drop_table("experience_reviews")
    op.execute("DROP FUNCTION protect_experience_review_fact()")
    op.drop_constraint("uq_experience_candidate_identity", "experience_candidates", type_="unique")
