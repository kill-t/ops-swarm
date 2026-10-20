"""新增持久经验生成任务、执行尝试及不可变候选。"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0062_experience_candidates"
down_revision: str | None = "0061_trusted_operator_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "experience_generation_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_version", sa.String(40), nullable=False),
        sa.Column("source", postgresql.JSONB(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("requested_by_id", sa.String(100), nullable=False),
        sa.Column("authorization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True)),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["report_id"], ["incident_reports.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["authorization_id"], ["operator_authorizations.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("report_id", "policy_version", name="uq_experience_jobs_report_policy"),
        sa.CheckConstraint(
            "policy_version = 'report-fact-draft-v1'", name="ck_experience_jobs_policy"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_experience_jobs_status",
        ),
        sa.CheckConstraint(
            "max_attempts = 3 AND attempt_count BETWEEN 0 AND max_attempts",
            name="ck_experience_jobs_attempts",
        ),
        sa.CheckConstraint("source_hash ~ '^[0-9a-f]{64}$'", name="ck_experience_jobs_hash"),
        sa.CheckConstraint(
            "jsonb_typeof(source) = 'object' AND "
            "COALESCE(source->>'report_id' = report_id::text, false)",
            name="ck_experience_jobs_source",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL AND locked_until IS NOT NULL) OR "
            "(status <> 'running' AND lease_token IS NULL AND locked_until IS NULL)",
            name="ck_experience_jobs_lease",
        ),
        sa.CheckConstraint(
            "(status IN ('queued', 'running') AND completed_at IS NULL) OR "
            "(status IN ('succeeded', 'failed') AND completed_at IS NOT NULL)",
            name="ck_experience_jobs_completion",
        ),
        sa.CheckConstraint(
            "status <> 'failed' OR last_error_code IS NOT NULL", name="ck_experience_jobs_error"
        ),
        sa.CheckConstraint(
            "created_at <= updated_at AND available_at >= created_at "
            "AND (completed_at IS NULL OR completed_at >= created_at)",
            name="ck_experience_jobs_time",
        ),
    )
    op.create_index(
        "ix_experience_jobs_claim",
        "experience_generation_jobs",
        ["status", "available_at", "created_at"],
    )
    op.create_table(
        "experience_generation_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(100), nullable=False),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["job_id"], ["experience_generation_jobs.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("job_id", "number", name="uq_experience_attempts_number"),
        sa.CheckConstraint("number BETWEEN 1 AND 3", name="ck_experience_attempts_number"),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'lease_expired')",
            name="ck_experience_attempts_status",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND error_code IS NULL) OR "
            "(status = 'succeeded' AND finished_at IS NOT NULL AND error_code IS NULL) OR "
            "(status IN ('failed', 'lease_expired') AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL)",
            name="ck_experience_attempts_completion",
        ),
        sa.CheckConstraint(
            "lease_expires_at > started_at AND (finished_at IS NULL OR finished_at >= started_at)",
            name="ck_experience_attempts_time",
        ),
    )
    op.create_table(
        "experience_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["experience_generation_jobs.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "version = 1 AND status = 'pending_review'",
            name="ck_experience_candidates_version_status",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$' AND source_hash ~ '^[0-9a-f]{64}$'",
            name="ck_experience_candidates_hashes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object'", name="ck_experience_candidates_content"
        ),
    )
    op.execute("""
        CREATE FUNCTION protect_experience_job() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION '不能删除已持久化的经验生成任务';
            END IF;
            IF OLD.status IN ('succeeded', 'failed') THEN
                RAISE EXCEPTION '不能修改已终态的经验生成任务';
            END IF;
            IF ROW(NEW.id, NEW.report_id, NEW.policy_version, NEW.source,
                   NEW.source_hash, NEW.requested_by_id, NEW.authorization_id,
                   NEW.max_attempts, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.report_id, OLD.policy_version, OLD.source,
                   OLD.source_hash, OLD.requested_by_id, OLD.authorization_id,
                   OLD.max_attempts, OLD.created_at) THEN
                RAISE EXCEPTION '经验生成任务的来源身份策略和请求不可变';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER experience_job_immutable_facts
            BEFORE UPDATE OR DELETE ON experience_generation_jobs
            FOR EACH ROW EXECUTE FUNCTION protect_experience_job();
    """)
    op.execute("""
        CREATE FUNCTION protect_experience_attempt() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION '不能删除已持久化的经验生成尝试';
            END IF;
            IF OLD.status <> 'running' THEN
                RAISE EXCEPTION '不能修改已终态的经验生成尝试';
            END IF;
            IF ROW(NEW.id, NEW.job_id, NEW.number, NEW.worker_id,
                   NEW.lease_token, NEW.started_at, NEW.lease_expires_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.job_id, OLD.number, OLD.worker_id,
                   OLD.lease_token, OLD.started_at, OLD.lease_expires_at) THEN
                RAISE EXCEPTION '经验生成尝试的身份与租约不可变';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER experience_attempt_immutable_facts
            BEFORE UPDATE OR DELETE ON experience_generation_attempts
            FOR EACH ROW EXECUTE FUNCTION protect_experience_attempt();
    """)
    op.execute("""
        CREATE FUNCTION protect_experience_candidate() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '经验候选版本不可修改或删除';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER experience_candidate_immutable
            BEFORE UPDATE OR DELETE ON experience_candidates
            FOR EACH ROW EXECUTE FUNCTION protect_experience_candidate();
    """)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM experience_generation_jobs)
               OR EXISTS (SELECT 1 FROM experience_generation_attempts)
               OR EXISTS (SELECT 1 FROM experience_candidates) THEN
                RAISE EXCEPTION '不能删除已持久化的经验生成或候选事实';
            END IF;
        END $$;
    """)
    op.drop_table("experience_candidates")
    op.drop_table("experience_generation_attempts")
    op.drop_index("ix_experience_jobs_claim", table_name="experience_generation_jobs")
    op.drop_table("experience_generation_jobs")
    op.execute("DROP FUNCTION protect_experience_candidate()")
    op.execute("DROP FUNCTION protect_experience_attempt()")
    op.execute("DROP FUNCTION protect_experience_job()")
