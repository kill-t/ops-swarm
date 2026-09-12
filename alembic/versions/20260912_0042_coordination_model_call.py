"""允许协调模型共享调用账本并让轮次计划引用其来源。"""

from alembic import op

revision = "0042_coordination_model_call"
down_revision = "0041_dynamic_coordination_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE investigation_call_reservations
        ADD CONSTRAINT uq_call_reservations_id_run
          UNIQUE (id, investigation_run_id);

      ALTER TABLE investigation_coordination_plans
        ADD COLUMN source_call_id UUID,
        ADD CONSTRAINT fk_coordination_plans_source_call
          FOREIGN KEY (source_call_id, investigation_run_id)
          REFERENCES investigation_call_reservations(id, investigation_run_id)
          ON DELETE RESTRICT;

      ALTER TABLE investigation_call_reservations
        DROP CONSTRAINT ck_call_reservations_loop_payload,
        DROP CONSTRAINT ck_call_reservations_invocation_index,
        DROP CONSTRAINT ck_call_reservations_operation,
        DROP CONSTRAINT ck_call_reservations_scope;

      ALTER TABLE investigation_call_reservations
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
             ((operation = 'coordinate' AND
               COALESCE(model_input ->> 'version', '') =
                 'coordination-model-input-v1' AND
               COALESCE(call_result ->> 'version', '') =
                 'coordination-model-result-v1') OR
              (operation <> 'coordinate' AND
               COALESCE(model_input ->> 'version', '') =
                 'evidence-loop-input-v1'))) OR
            (kind = 'tool' AND tool_request IS NOT NULL))))),
        ADD CONSTRAINT ck_call_reservations_invocation_index CHECK
        (invocation_index BETWEEN 0 AND 99 AND
         (operation <> 'coordinate' OR invocation_index = 0)),
        ADD CONSTRAINT ck_call_reservations_operation CHECK
        ((operation IN ('query_logs', 'query_metrics', 'list_changes',
                        'read_log_evidence', 'read_metric_evidence',
                        'read_change_evidence') AND kind = 'tool') OR
         (operation IN ('coordinate', 'analyze_log', 'analyze_metric',
                        'analyze_change', 'synthesize') AND kind = 'model')),
        ADD CONSTRAINT ck_call_reservations_scope CHECK
        ((operation IN ('coordinate', 'synthesize') AND task_id IS NULL AND
          task_attempt_id IS NULL) OR
         (operation NOT IN ('coordinate', 'synthesize') AND task_id IS NOT NULL AND
          task_attempt_id IS NOT NULL));
    """)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_call_reservations
                   WHERE operation = 'coordinate')
           OR EXISTS (SELECT 1 FROM investigation_coordination_plans
                      WHERE source_call_id IS NOT NULL) THEN
          RAISE EXCEPTION '不能删除已保存的协调模型调用或计划来源';
        END IF;
      END $$;

      ALTER TABLE investigation_coordination_plans
        DROP CONSTRAINT fk_coordination_plans_source_call,
        DROP COLUMN source_call_id;

      ALTER TABLE investigation_call_reservations
        DROP CONSTRAINT ck_call_reservations_loop_payload,
        DROP CONSTRAINT ck_call_reservations_invocation_index,
        DROP CONSTRAINT ck_call_reservations_operation,
        DROP CONSTRAINT ck_call_reservations_scope;

      ALTER TABLE investigation_call_reservations
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
            (kind = 'tool' AND tool_request IS NOT NULL))))),
        ADD CONSTRAINT ck_call_reservations_invocation_index CHECK
        (invocation_index BETWEEN 0 AND 99),
        ADD CONSTRAINT ck_call_reservations_operation CHECK
        ((operation IN ('query_logs', 'query_metrics', 'list_changes',
                        'read_log_evidence', 'read_metric_evidence',
                        'read_change_evidence') AND kind = 'tool') OR
         (operation IN ('analyze_log', 'analyze_metric', 'analyze_change', 'synthesize')
          AND kind = 'model')),
        ADD CONSTRAINT ck_call_reservations_scope CHECK
        ((operation = 'synthesize' AND task_id IS NULL AND task_attempt_id IS NULL) OR
         (operation <> 'synthesize' AND task_id IS NOT NULL AND task_attempt_id IS NOT NULL));

      ALTER TABLE investigation_call_reservations
        DROP CONSTRAINT uq_call_reservations_id_run;
    """)
