"""增加独立 v3 启动契约并冻结动态工作流恢复策略。"""

from alembic import op

revision = "0043_dynamic_workflow_start"
down_revision = "0042_coordination_model_call"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE investigation_runs
        ADD COLUMN graph_version VARCHAR(64),
        ADD COLUMN coordination_policy_version VARCHAR(32);

      ALTER TABLE investigation_runs
        DROP CONSTRAINT ck_investigation_runs_context_policy,
        DROP CONSTRAINT ck_investigation_runs_evidence_policy,
        DROP CONSTRAINT ck_investigation_runs_model_budget,
        DROP CONSTRAINT ck_investigation_runs_call_budget,
        DROP CONSTRAINT ck_investigation_runs_workflow_version;

      ALTER TABLE investigation_runs
        ADD CONSTRAINT ck_investigation_runs_context_policy CHECK
        (context_policy_version IS NULL OR (context_policy_version IN
         ('task-context-v1', 'evidence-read-v1', 'summary-context-v1') AND
         workflow_version IN ('v2', 'v3') AND model_budget IS NOT NULL AND
         evidence_policy_version IS NOT NULL AND
         (context_policy_version = 'task-context-v1' OR
          COALESCE(model_budget #>> '{profile,structured_output_method}', '') =
            'function_calling'))),
        ADD CONSTRAINT ck_investigation_runs_evidence_policy CHECK
        (evidence_policy_version IS NULL OR
         (workflow_version IN ('v2', 'v3') AND call_budget IS NOT NULL AND
          ((evidence_policy_version = 'task-evidence-v1' AND
            data_source_mode = 'frozen') OR
           (evidence_policy_version = 'task-evidence-live-v1' AND
            data_source_mode = 'live')))),
        ADD CONSTRAINT ck_investigation_runs_model_budget CHECK
        (model_budget IS NULL OR
         (workflow_version IN ('v2', 'v3') AND call_budget IS NOT NULL AND
          jsonb_typeof(model_budget) = 'object' AND
          octet_length(model_budget::text) <= 8192)),
        ADD CONSTRAINT ck_investigation_runs_call_budget CHECK
        (call_budget IS NULL OR
         (workflow_version IN ('v2', 'v3') AND
          jsonb_typeof(call_budget) = 'object' AND
          octet_length(call_budget::text) <= 8192)),
        ADD CONSTRAINT ck_investigation_runs_workflow_version CHECK
        (workflow_version IN ('v1', 'v2', 'v3') AND
         (workflow_version = 'v1' OR mode = 'multi_agent')),
        ADD CONSTRAINT ck_investigation_runs_dynamic_policy CHECK
        ((workflow_version <> 'v3' AND graph_version IS NULL AND
          coordination_policy_version IS NULL) OR
         (workflow_version = 'v3' AND data_source_mode = 'frozen' AND
          COALESCE(graph_version, '') = 'dynamic-investigation-graph-v1' AND
          COALESCE(coordination_policy_version, '') = 'dynamic-coordination-v1' AND
          call_budget IS NOT NULL AND model_budget IS NOT NULL AND
          COALESCE(context_policy_version, '') = 'summary-context-v1' AND
          COALESCE(working_summary_policy_version, '') = 'task-working-summary-v1' AND
          COALESCE(evidence_policy_version, '') = 'task-evidence-v1' AND
          prompt_version = 'summary-context-v1' AND
          COALESCE(model_budget #>> '{profile,structured_output_method}', '') =
            'function_calling'));
    """)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_runs
                   WHERE workflow_version = 'v3'
                      OR graph_version IS NOT NULL
                      OR coordination_policy_version IS NOT NULL) THEN
          RAISE EXCEPTION '不能删除已保存的 v3 动态工作流启动契约';
        END IF;
      END $$;

      ALTER TABLE investigation_runs
        DROP CONSTRAINT ck_investigation_runs_dynamic_policy,
        DROP CONSTRAINT ck_investigation_runs_context_policy,
        DROP CONSTRAINT ck_investigation_runs_evidence_policy,
        DROP CONSTRAINT ck_investigation_runs_model_budget,
        DROP CONSTRAINT ck_investigation_runs_call_budget,
        DROP CONSTRAINT ck_investigation_runs_workflow_version;

      ALTER TABLE investigation_runs
        ADD CONSTRAINT ck_investigation_runs_context_policy CHECK
        (context_policy_version IS NULL OR (context_policy_version IN
         ('task-context-v1', 'evidence-read-v1', 'summary-context-v1') AND
         workflow_version = 'v2' AND model_budget IS NOT NULL AND
         evidence_policy_version IS NOT NULL AND
         (context_policy_version = 'task-context-v1' OR
          COALESCE(model_budget #>> '{profile,structured_output_method}', '') =
            'function_calling'))),
        ADD CONSTRAINT ck_investigation_runs_evidence_policy CHECK
        (evidence_policy_version IS NULL OR (workflow_version = 'v2' AND
         call_budget IS NOT NULL AND
         ((evidence_policy_version = 'task-evidence-v1' AND
           data_source_mode = 'frozen') OR
          (evidence_policy_version = 'task-evidence-live-v1' AND
           data_source_mode = 'live')))),
        ADD CONSTRAINT ck_investigation_runs_model_budget CHECK
        (model_budget IS NULL OR (workflow_version = 'v2' AND
         call_budget IS NOT NULL AND jsonb_typeof(model_budget) = 'object' AND
         octet_length(model_budget::text) <= 8192)),
        ADD CONSTRAINT ck_investigation_runs_call_budget CHECK
        (call_budget IS NULL OR (workflow_version = 'v2' AND
         jsonb_typeof(call_budget) = 'object' AND
         octet_length(call_budget::text) <= 8192)),
        ADD CONSTRAINT ck_investigation_runs_workflow_version CHECK
        (workflow_version IN ('v1', 'v2') AND
         (workflow_version = 'v1' OR mode = 'multi_agent'));

      ALTER TABLE investigation_runs
        DROP COLUMN coordination_policy_version,
        DROP COLUMN graph_version;
    """)
