"""允许综合节点显式消费已完成任务的工作摘要。"""

from alembic import op

revision = "0040_summary_context"
down_revision = "0039_task_working_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE investigation_runs
        DROP CONSTRAINT ck_investigation_runs_working_summary_policy,
        DROP CONSTRAINT ck_investigation_runs_context_policy;

      ALTER TABLE investigation_runs
        ADD CONSTRAINT ck_investigation_runs_working_summary_policy CHECK
        ((working_summary_policy_version IS NULL AND
          context_policy_version IS DISTINCT FROM 'summary-context-v1') OR
         (COALESCE(working_summary_policy_version, '') = 'task-working-summary-v1' AND
          COALESCE(context_policy_version, '') IN
            ('evidence-read-v1', 'summary-context-v1'))),
        ADD CONSTRAINT ck_investigation_runs_context_policy CHECK
        (context_policy_version IS NULL OR (context_policy_version IN
         ('task-context-v1', 'evidence-read-v1', 'summary-context-v1') AND
         workflow_version = 'v2' AND model_budget IS NOT NULL AND
         evidence_policy_version IS NOT NULL AND
         (context_policy_version = 'task-context-v1' OR
          COALESCE(model_budget #>> '{profile,structured_output_method}', '') =
            'function_calling')));
    """)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_runs
                   WHERE context_policy_version = 'summary-context-v1'
                      OR prompt_version = 'summary-context-v1')
           OR EXISTS (SELECT 1 FROM investigation_call_reservations
                      WHERE context_manifest ->> 'version' = 'summary-context-v1'
                         OR context_manifest ->> 'prompt_version' =
                           'summary-context-v1'
                         OR model_allocation ->> 'prompt_version' =
                           'summary-context-v1') THEN
          RAISE EXCEPTION '不能删除已保存的摘要上下文策略或调用材料清单';
        END IF;
      END $$;

      ALTER TABLE investigation_runs
        DROP CONSTRAINT ck_investigation_runs_working_summary_policy,
        DROP CONSTRAINT ck_investigation_runs_context_policy;

      ALTER TABLE investigation_runs
        ADD CONSTRAINT ck_investigation_runs_working_summary_policy CHECK
        (working_summary_policy_version IS NULL OR
         (working_summary_policy_version = 'task-working-summary-v1' AND
          context_policy_version IS NOT NULL AND
          context_policy_version = 'evidence-read-v1')),
        ADD CONSTRAINT ck_investigation_runs_context_policy CHECK
        (context_policy_version IS NULL OR (context_policy_version IN
         ('task-context-v1', 'evidence-read-v1') AND workflow_version = 'v2' AND
         model_budget IS NOT NULL AND evidence_policy_version IS NOT NULL AND
         (context_policy_version = 'task-context-v1' OR
          COALESCE(model_budget #>> '{profile,structured_output_method}', '') =
            'function_calling')));
    """)
