"""发布第三轮专业调查图 v12。"""

from alembic import op

revision = "0054_third_round_investigation"
down_revision = "0053_third_round_planning"
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
            ('dynamic-investigation-graph-v1', 'dynamic-investigation-graph-v2',
             'dynamic-investigation-graph-v3', 'dynamic-investigation-graph-v4',
             'dynamic-investigation-graph-v5', 'dynamic-investigation-graph-v6',
             'dynamic-investigation-graph-v7', 'dynamic-investigation-graph-v8',
             'dynamic-investigation-graph-v9', 'dynamic-investigation-graph-v10',
             'dynamic-investigation-graph-v11', 'dynamic-investigation-graph-v12') AND
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
                   WHERE graph_version = 'dynamic-investigation-graph-v12') THEN
          RAISE EXCEPTION '不能删除已保存的第三轮专业调查图';
        END IF;
        IF EXISTS (
          SELECT 1
          FROM agent_task_attempts attempt
          JOIN agent_tasks task ON task.id = attempt.task_id
          JOIN investigation_rounds round_row ON round_row.id = task.round_id
          WHERE round_row.round_number >= 3
        ) THEN
          RAISE EXCEPTION '不能删除已保存的第三轮专业执行事实';
        END IF;
      END $$;

      ALTER TABLE investigation_runs
        DROP CONSTRAINT ck_investigation_runs_dynamic_policy;

      ALTER TABLE investigation_runs
        ADD CONSTRAINT ck_investigation_runs_dynamic_policy CHECK
        ((workflow_version <> 'v3' AND graph_version IS NULL AND
          coordination_policy_version IS NULL) OR
         (workflow_version = 'v3' AND data_source_mode = 'frozen' AND
          COALESCE(graph_version, '') IN
            ('dynamic-investigation-graph-v1', 'dynamic-investigation-graph-v2',
             'dynamic-investigation-graph-v3', 'dynamic-investigation-graph-v4',
             'dynamic-investigation-graph-v5', 'dynamic-investigation-graph-v6',
             'dynamic-investigation-graph-v7', 'dynamic-investigation-graph-v8',
             'dynamic-investigation-graph-v9', 'dynamic-investigation-graph-v10',
             'dynamic-investigation-graph-v11') AND
          COALESCE(coordination_policy_version, '') = 'dynamic-coordination-v1' AND
          call_budget IS NOT NULL AND model_budget IS NOT NULL AND
          COALESCE(context_policy_version, '') = 'summary-context-v1' AND
          COALESCE(working_summary_policy_version, '') = 'task-working-summary-v1' AND
          COALESCE(evidence_policy_version, '') = 'task-evidence-v1' AND
          prompt_version = 'summary-context-v1' AND
          COALESCE(model_budget #>> '{profile,structured_output_method}', '') =
            'function_calling'));
    """)
