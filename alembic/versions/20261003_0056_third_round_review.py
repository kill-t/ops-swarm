"""发布第三轮独立审查图 v14。"""

from alembic import op

revision = "0056_third_round_review"
down_revision = "0055_third_round_synthesis"
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
             'dynamic-investigation-graph-v13', 'dynamic-investigation-graph-v14') AND
          COALESCE(coordination_policy_version, '') = 'dynamic-coordination-v1' AND
          call_budget IS NOT NULL AND model_budget IS NOT NULL AND
          COALESCE(context_policy_version, '') = 'summary-context-v1' AND
          COALESCE(working_summary_policy_version, '') = 'task-working-summary-v1' AND
          COALESCE(evidence_policy_version, '') = 'task-evidence-v1' AND
          prompt_version = 'summary-context-v1' AND
          COALESCE(model_budget #>> '{profile,structured_output_method}', '') =
            'function_calling'));

      ALTER TABLE investigation_call_reservations
        DROP CONSTRAINT ck_call_reservations_invocation_index;

      ALTER TABLE investigation_call_reservations
        ADD CONSTRAINT ck_call_reservations_invocation_index CHECK
        (invocation_index BETWEEN 0 AND 99 AND
         (operation <> 'coordinate' OR invocation_index = 0) AND
         (operation <> 'review' OR invocation_index IN (0, 1, 2)));
    """)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_runs
                   WHERE graph_version = 'dynamic-investigation-graph-v14') THEN
          RAISE EXCEPTION '不能删除已保存的第三轮独立审查图';
        END IF;
        IF EXISTS (
          SELECT 1
          FROM investigation_call_reservations
          WHERE operation = 'review' AND invocation_index >= 2
        ) THEN
          RAISE EXCEPTION '不能删除已保存的第三轮独立审查调用事实';
        END IF;
        IF EXISTS (
          SELECT 1
          FROM investigation_hypothesis_reviews review
          JOIN investigation_rounds round_row ON round_row.id = review.round_id
          WHERE round_row.round_number >= 3
        ) THEN
          RAISE EXCEPTION '不能删除已保存的第三轮独立审查事实';
        END IF;
      END $$;

      ALTER TABLE investigation_call_reservations
        DROP CONSTRAINT ck_call_reservations_invocation_index;

      ALTER TABLE investigation_call_reservations
        ADD CONSTRAINT ck_call_reservations_invocation_index CHECK
        (invocation_index BETWEEN 0 AND 99 AND
         (operation <> 'coordinate' OR invocation_index = 0) AND
         (operation <> 'review' OR invocation_index IN (0, 1)));

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
