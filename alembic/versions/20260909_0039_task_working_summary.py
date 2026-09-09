"""保存已完成专业任务的不可变结构化工作摘要。"""

from alembic import op

revision = "0039_task_working_summary"
down_revision = "0038_evidence_read_loop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE investigation_runs
        ADD COLUMN working_summary_policy_version VARCHAR(32),
        ADD CONSTRAINT ck_investigation_runs_working_summary_policy CHECK
        (working_summary_policy_version IS NULL OR
         (working_summary_policy_version = 'task-working-summary-v1' AND
          context_policy_version IS NOT NULL AND
          context_policy_version = 'evidence-read-v1'));

      CREATE TABLE agent_task_working_summaries (
        task_id UUID PRIMARY KEY REFERENCES agent_task_results(task_id) ON DELETE RESTRICT,
        investigation_run_id UUID NOT NULL,
        attempt_id UUID NOT NULL,
        version VARCHAR(32) NOT NULL,
        summary JSONB NOT NULL,
        content_sha256 VARCHAR(64) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT fk_task_working_summaries_attempt
          FOREIGN KEY (attempt_id, task_id, investigation_run_id)
          REFERENCES agent_task_attempts(id, task_id, investigation_run_id)
          ON DELETE RESTRICT,
        CONSTRAINT ck_task_working_summaries_version CHECK
          (version = 'task-working-summary-v1'),
        CONSTRAINT ck_task_working_summaries_size CHECK
          (jsonb_typeof(summary) = 'object' AND octet_length(summary::text) <= 262144),
        CONSTRAINT ck_task_working_summaries_hash CHECK
          (content_sha256 ~ '^[0-9a-f]{64}$')
      );

      CREATE FUNCTION reject_task_working_summary_mutation()
      RETURNS TRIGGER
      LANGUAGE plpgsql
      AS $$
      BEGIN
        RAISE EXCEPTION '专业任务工作摘要不可更新或删除';
      END;
      $$;

      CREATE TRIGGER trg_task_working_summaries_immutable
      BEFORE UPDATE OR DELETE ON agent_task_working_summaries
      FOR EACH ROW EXECUTE FUNCTION reject_task_working_summary_mutation();
    """)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM agent_task_working_summaries)
           OR EXISTS (SELECT 1 FROM investigation_runs
                      WHERE working_summary_policy_version IS NOT NULL) THEN
          RAISE EXCEPTION '不能删除已保存的专业任务工作摘要或已固定的摘要策略';
        END IF;
      END $$;

      DROP TRIGGER trg_task_working_summaries_immutable
        ON agent_task_working_summaries;
      DROP FUNCTION reject_task_working_summary_mutation();
      DROP TABLE agent_task_working_summaries;

      ALTER TABLE investigation_runs
        DROP CONSTRAINT ck_investigation_runs_working_summary_policy,
        DROP COLUMN working_summary_policy_version;
    """)
