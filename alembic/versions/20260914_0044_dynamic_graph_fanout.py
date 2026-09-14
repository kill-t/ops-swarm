"""为动态首轮专业分派增加独立图恢复版本。"""

from alembic import op

revision = "0044_dynamic_graph_fanout"
down_revision = "0043_dynamic_workflow_start"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE investigation_runs
        DROP CONSTRAINT ck_investigation_runs_dynamic_policy;

      ALTER TABLE investigation_runs
        ADD CONSTRAINT ck_investigation_runs_dynamic_policy CHECK
        ((workflow_version <> 'v3' AND graph_version IS NULL AND
          coordination_policy_version IS NULL) OR
         (workflow_version = 'v3' AND data_source_mode = 'frozen' AND
          COALESCE(graph_version, '') IN
            ('dynamic-investigation-graph-v1', 'dynamic-investigation-graph-v2') AND
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
                   WHERE graph_version = 'dynamic-investigation-graph-v2') THEN
          RAISE EXCEPTION '不能删除已保存的动态专业分派图版本';
        END IF;
      END $$;

      ALTER TABLE investigation_runs
        DROP CONSTRAINT ck_investigation_runs_dynamic_policy;

      ALTER TABLE investigation_runs
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
