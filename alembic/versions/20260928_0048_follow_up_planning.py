"""保存确定性补查计划来源并发布动态图 v6。"""

from alembic import op

revision = "0048_follow_up_planning"
down_revision = "0047_review_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE investigation_hypothesis_review_routes
        ADD CONSTRAINT uq_hypothesis_review_routes_id_run
        UNIQUE (id, investigation_run_id);

      ALTER TABLE investigation_coordination_plans
        ADD COLUMN source_review_route_id UUID NULL,
        ADD CONSTRAINT fk_coordination_plans_source_review_route
          FOREIGN KEY (source_review_route_id, investigation_run_id)
          REFERENCES investigation_hypothesis_review_routes(id, investigation_run_id)
          ON DELETE RESTRICT,
        ADD CONSTRAINT uq_coordination_plans_source_review_route
          UNIQUE (source_review_route_id, investigation_run_id),
        ADD CONSTRAINT ck_coordination_plans_source_exclusive CHECK
          (source_call_id IS NULL OR source_review_route_id IS NULL);

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
             'dynamic-investigation-graph-v5', 'dynamic-investigation-graph-v6') AND
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
        IF EXISTS (SELECT 1 FROM investigation_coordination_plans
                   WHERE source_review_route_id IS NOT NULL)
           OR EXISTS (SELECT 1 FROM investigation_runs
                      WHERE graph_version = 'dynamic-investigation-graph-v6') THEN
          RAISE EXCEPTION '不能删除已保存的补查计划图或补查计划来源';
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
             'dynamic-investigation-graph-v5') AND
          COALESCE(coordination_policy_version, '') = 'dynamic-coordination-v1' AND
          call_budget IS NOT NULL AND model_budget IS NOT NULL AND
          COALESCE(context_policy_version, '') = 'summary-context-v1' AND
          COALESCE(working_summary_policy_version, '') = 'task-working-summary-v1' AND
          COALESCE(evidence_policy_version, '') = 'task-evidence-v1' AND
          prompt_version = 'summary-context-v1' AND
          COALESCE(model_budget #>> '{profile,structured_output_method}', '') =
            'function_calling'));

      ALTER TABLE investigation_coordination_plans
        DROP CONSTRAINT ck_coordination_plans_source_exclusive,
        DROP CONSTRAINT uq_coordination_plans_source_review_route,
        DROP CONSTRAINT fk_coordination_plans_source_review_route,
        DROP COLUMN source_review_route_id;

      ALTER TABLE investigation_hypothesis_review_routes
        DROP CONSTRAINT uq_hypothesis_review_routes_id_run;
    """)
