"""发布第三轮综合图 v13。"""

from alembic import op

revision = "0055_third_round_synthesis"
down_revision = "0054_third_round_investigation"
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
             'dynamic-investigation-graph-v11', 'dynamic-investigation-graph-v12',
             'dynamic-investigation-graph-v13') AND
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
                   WHERE graph_version = 'dynamic-investigation-graph-v13') THEN
          RAISE EXCEPTION '不能删除已保存的第三轮综合图';
        END IF;
        IF EXISTS (
          SELECT 1
          FROM investigation_call_reservations
          WHERE operation = 'synthesize' AND invocation_index >= 2
        ) THEN
          RAISE EXCEPTION '不能删除已保存的第三轮综合调用事实';
        END IF;
        IF EXISTS (
          SELECT 1
          FROM investigation_open_hypotheses hypothesis
          JOIN investigation_rounds round_row ON round_row.id = hypothesis.round_id
          WHERE round_row.round_number >= 3
        ) THEN
          RAISE EXCEPTION '不能删除已保存的第三轮开放假设';
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
