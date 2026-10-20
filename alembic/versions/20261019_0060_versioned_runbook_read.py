"""增加版本化调查手册读取策略与持久调用边界。

Revision ID: 0060_versioned_runbook_read
Revises: 0059_telemetry_mcp_metric_policy
Create Date: 2026-10-19
"""

from alembic import op

revision = "0060_versioned_runbook_read"
down_revision = "0059_telemetry_mcp_metric_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE investigation_runs
          DROP CONSTRAINT ck_investigation_runs_context_policy;
        ALTER TABLE investigation_runs
          ADD CONSTRAINT ck_investigation_runs_context_policy CHECK
          (context_policy_version IS NULL OR (context_policy_version IN
           ('task-context-v1', 'evidence-read-v1', 'summary-context-v1',
            'runbook-read-v1') AND
           workflow_version IN ('v2', 'v3') AND model_budget IS NOT NULL AND
           evidence_policy_version IS NOT NULL AND
           (context_policy_version <> 'runbook-read-v1' OR workflow_version = 'v2') AND
           (context_policy_version = 'task-context-v1' OR
            COALESCE(model_budget #>> '{profile,structured_output_method}', '') =
              'function_calling')));

        ALTER TABLE investigation_call_reservations
          DROP CONSTRAINT ck_call_reservations_loop_payload,
          DROP CONSTRAINT ck_call_reservations_operation;
        ALTER TABLE investigation_call_reservations
          ADD CONSTRAINT ck_call_reservations_loop_payload CHECK
          (((operation IN ('read_log_evidence','read_metric_evidence',
                           'read_change_evidence','read_log_runbook',
                           'read_metric_runbook','read_change_runbook'))
            = (tool_request IS NOT NULL)) AND
           (tool_request IS NULL OR (jsonb_typeof(tool_request) = 'object' AND
            octet_length(tool_request::text) <= 8192)) AND
           (call_result IS NULL OR (status <> 'reserved' AND
            jsonb_typeof(call_result) = 'object' AND
            octet_length(call_result::text) <= 65536 AND
            COALESCE(call_result ->> 'kind', '') = kind AND
            ((kind = 'model' AND
              ((operation = 'coordinate' AND
                COALESCE(model_input ->> 'version', '') =
                  'coordination-model-input-v1' AND
                COALESCE(call_result ->> 'version', '') =
                  'coordination-model-result-v1') OR
               (operation = 'synthesize' AND NOT (model_input ? 'version') AND
                COALESCE(call_result ->> 'version', '') =
                  'open-hypothesis-synthesis-result-v1') OR
               (operation = 'review' AND NOT (model_input ? 'version') AND
                COALESCE(call_result ->> 'version', '') =
                  'hypothesis-review-result-v1') OR
               (operation NOT IN ('coordinate', 'synthesize', 'review') AND
                COALESCE(model_input ->> 'version', '') IN
                  ('evidence-loop-input-v1','runbook-loop-input-v1')))) OR
             (kind = 'tool' AND tool_request IS NOT NULL))))),
          ADD CONSTRAINT ck_call_reservations_operation CHECK
          ((operation IN ('query_logs', 'query_metrics', 'list_changes',
                          'read_log_evidence', 'read_metric_evidence',
                          'read_change_evidence', 'read_log_runbook',
                          'read_metric_runbook', 'read_change_runbook') AND kind = 'tool') OR
           (operation IN ('coordinate', 'analyze_log', 'analyze_metric',
                          'analyze_change', 'synthesize', 'review') AND kind = 'model'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM investigation_runs
            WHERE context_policy_version = 'runbook-read-v1'
               OR prompt_version = 'runbook-read-v1'
          ) OR EXISTS (
            SELECT 1 FROM investigation_call_reservations
            WHERE model_input ->> 'version' = 'runbook-loop-input-v1'
               OR context_manifest ->> 'version' = 'runbook-read-v1'
               OR context_manifest ->> 'prompt_version' = 'runbook-read-v1'
               OR operation IN ('read_log_runbook', 'read_metric_runbook',
                                'read_change_runbook')
               OR tool_request #>> '{arguments,runbook_id}' IS NOT NULL
               OR call_result #>> '{message,name}' = 'read_runbook'
               OR call_result #>> '{messages,0,tool_calls,0,name}' = 'read_runbook'
          ) THEN
            RAISE EXCEPTION '不能删除已保存的调查手册读取策略或调用审计';
          END IF;
        END $$;

        ALTER TABLE investigation_call_reservations
          DROP CONSTRAINT ck_call_reservations_loop_payload,
          DROP CONSTRAINT ck_call_reservations_operation;
        ALTER TABLE investigation_call_reservations
          ADD CONSTRAINT ck_call_reservations_loop_payload CHECK
          (((operation IN ('read_log_evidence','read_metric_evidence','read_change_evidence'))
            = (tool_request IS NOT NULL)) AND
           (tool_request IS NULL OR (jsonb_typeof(tool_request) = 'object' AND
            octet_length(tool_request::text) <= 8192)) AND
           (call_result IS NULL OR (status <> 'reserved' AND
            jsonb_typeof(call_result) = 'object' AND
            octet_length(call_result::text) <= 65536 AND
            COALESCE(call_result ->> 'kind', '') = kind AND
            ((kind = 'model' AND
              ((operation = 'coordinate' AND
                COALESCE(model_input ->> 'version', '') =
                  'coordination-model-input-v1' AND
                COALESCE(call_result ->> 'version', '') =
                  'coordination-model-result-v1') OR
               (operation = 'synthesize' AND NOT (model_input ? 'version') AND
                COALESCE(call_result ->> 'version', '') =
                  'open-hypothesis-synthesis-result-v1') OR
               (operation = 'review' AND NOT (model_input ? 'version') AND
                COALESCE(call_result ->> 'version', '') =
                  'hypothesis-review-result-v1') OR
               (operation NOT IN ('coordinate', 'synthesize', 'review') AND
                COALESCE(model_input ->> 'version', '') =
                  'evidence-loop-input-v1'))) OR
             (kind = 'tool' AND tool_request IS NOT NULL))))),
          ADD CONSTRAINT ck_call_reservations_operation CHECK
          ((operation IN ('query_logs', 'query_metrics', 'list_changes',
                          'read_log_evidence', 'read_metric_evidence',
                          'read_change_evidence') AND kind = 'tool') OR
           (operation IN ('coordinate', 'analyze_log', 'analyze_metric',
                          'analyze_change', 'synthesize', 'review') AND kind = 'model'));

        ALTER TABLE investigation_runs
          DROP CONSTRAINT ck_investigation_runs_context_policy;
        ALTER TABLE investigation_runs
          ADD CONSTRAINT ck_investigation_runs_context_policy CHECK
          (context_policy_version IS NULL OR (context_policy_version IN
           ('task-context-v1', 'evidence-read-v1', 'summary-context-v1') AND
           workflow_version IN ('v2', 'v3') AND model_budget IS NOT NULL AND
           evidence_policy_version IS NOT NULL AND
           (context_policy_version = 'task-context-v1' OR
            COALESCE(model_budget #>> '{profile,structured_output_method}', '') =
              'function_calling')));
        """
    )
