"""发布第三轮最大轮次停止路由图 v15。"""

from alembic import op

revision = "0057_third_round_routing"
down_revision = "0056_third_round_review"
branch_labels = None
depends_on = None


_DYNAMIC_POLICY_PREFIX = """
      ALTER TABLE investigation_runs
        DROP CONSTRAINT ck_investigation_runs_dynamic_policy;

      ALTER TABLE investigation_runs
        ADD CONSTRAINT ck_investigation_runs_dynamic_policy CHECK
        ((workflow_version <> 'v3' AND graph_version IS NULL AND
          coordination_policy_version IS NULL) OR
         (workflow_version = 'v3' AND data_source_mode = 'frozen' AND
          COALESCE(graph_version, '') IN
"""
_DYNAMIC_POLICY_SUFFIX = """
          COALESCE(coordination_policy_version, '') = 'dynamic-coordination-v1' AND
          call_budget IS NOT NULL AND model_budget IS NOT NULL AND
          COALESCE(context_policy_version, '') = 'summary-context-v1' AND
          COALESCE(working_summary_policy_version, '') = 'task-working-summary-v1' AND
          COALESCE(evidence_policy_version, '') = 'task-evidence-v1' AND
          prompt_version = 'summary-context-v1' AND
          COALESCE(model_budget #>> '{profile,structured_output_method}', '') =
            'function_calling'));
"""


def _graph_versions(last: int) -> str:
    return (
        "("
        + ", ".join(f"'dynamic-investigation-graph-v{number}'" for number in range(1, last + 1))
        + ") AND\n"
    )


def upgrade() -> None:
    op.execute(_DYNAMIC_POLICY_PREFIX + _graph_versions(15) + _DYNAMIC_POLICY_SUFFIX)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_runs
                   WHERE graph_version = 'dynamic-investigation-graph-v15') THEN
          RAISE EXCEPTION '不能删除已保存的第三轮最大轮次停止路由图';
        END IF;
        IF EXISTS (
          SELECT 1
          FROM investigation_hypothesis_review_routes route
          JOIN investigation_rounds round_row ON round_row.id = route.round_id
          WHERE round_row.round_number >= 3
        ) THEN
          RAISE EXCEPTION '不能删除已保存的第三轮最大轮次停止路由事实';
        END IF;
      END $$;
    """)
    op.execute(_DYNAMIC_POLICY_PREFIX + _graph_versions(14) + _DYNAMIC_POLICY_SUFFIX)
