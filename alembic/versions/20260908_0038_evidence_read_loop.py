"""固定按需证据读取策略，并保存完整的工具交互。"""

from alembic import op

revision = "0038_evidence_read_loop"
down_revision = "0037_call_invocation_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE investigation_runs
        DROP CONSTRAINT ck_investigation_runs_context_policy;
      ALTER TABLE investigation_runs
        ADD CONSTRAINT ck_investigation_runs_context_policy CHECK
        (context_policy_version IS NULL OR (context_policy_version IN
         ('task-context-v1', 'evidence-read-v1') AND workflow_version = 'v2' AND
         model_budget IS NOT NULL AND evidence_policy_version IS NOT NULL AND
         (context_policy_version = 'task-context-v1' OR
          COALESCE(model_budget #>> '{profile,structured_output_method}', '') =
            'function_calling')));

      ALTER TABLE investigation_call_reservations
        ADD COLUMN tool_request JSONB,
        ADD COLUMN call_result JSONB;
      ALTER TABLE investigation_call_reservations
        DROP CONSTRAINT ck_call_reservations_operation;
      ALTER TABLE investigation_call_reservations
        ADD CONSTRAINT ck_call_reservations_operation CHECK
        ((operation IN ('query_logs', 'query_metrics', 'list_changes',
                        'read_log_evidence', 'read_metric_evidence',
                        'read_change_evidence') AND kind = 'tool') OR
         (operation IN ('analyze_log', 'analyze_metric', 'analyze_change', 'synthesize')
          AND kind = 'model')),
        ADD CONSTRAINT ck_call_reservations_loop_payload CHECK
        (((operation IN ('read_log_evidence', 'read_metric_evidence',
                         'read_change_evidence')) = (tool_request IS NOT NULL)) AND
         (tool_request IS NULL OR
          (jsonb_typeof(tool_request) = 'object' AND
           octet_length(tool_request::text) <= 8192)) AND
         (call_result IS NULL OR
          (status <> 'reserved' AND jsonb_typeof(call_result) = 'object' AND
           octet_length(call_result::text) <= 65536 AND
           COALESCE(call_result ->> 'kind', '') = kind AND
           ((kind = 'model' AND
             COALESCE(model_input ->> 'version', '') = 'evidence-loop-input-v1') OR
            (kind = 'tool' AND tool_request IS NOT NULL)))));
    """)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_runs
                   WHERE context_policy_version = 'evidence-read-v1')
           OR EXISTS (SELECT 1 FROM investigation_call_reservations
                      WHERE model_input ->> 'version' = 'evidence-loop-input-v1'
                         OR tool_request IS NOT NULL
                         OR call_result IS NOT NULL
                         OR operation IN ('read_log_evidence', 'read_metric_evidence',
                                          'read_change_evidence')) THEN
          RAISE EXCEPTION '不能删除已保存的证据读取策略或调用交互审计';
        END IF;
      END $$;

      ALTER TABLE investigation_call_reservations
        DROP CONSTRAINT ck_call_reservations_loop_payload,
        DROP CONSTRAINT ck_call_reservations_operation;
      ALTER TABLE investigation_call_reservations
        ADD CONSTRAINT ck_call_reservations_operation CHECK
        ((operation IN ('query_logs', 'query_metrics', 'list_changes') AND kind = 'tool') OR
         (operation IN ('analyze_log', 'analyze_metric', 'analyze_change', 'synthesize')
          AND kind = 'model'));
      ALTER TABLE investigation_call_reservations
        DROP COLUMN call_result,
        DROP COLUMN tool_request;

      ALTER TABLE investigation_runs
        DROP CONSTRAINT ck_investigation_runs_context_policy;
      ALTER TABLE investigation_runs
        ADD CONSTRAINT ck_investigation_runs_context_policy CHECK
        (context_policy_version IS NULL OR (context_policy_version = 'task-context-v1' AND
         workflow_version = 'v2' AND model_budget IS NOT NULL AND
         evidence_policy_version IS NOT NULL));
    """)
