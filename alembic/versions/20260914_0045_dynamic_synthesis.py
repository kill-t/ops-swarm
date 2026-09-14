"""保存动态首轮综合的开放假设与模型调用来源。"""

from alembic import op

revision = "0045_dynamic_synthesis"
down_revision = "0044_dynamic_graph_fanout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE investigation_coordination_plans
        ADD CONSTRAINT uq_coordination_plans_round_run
        UNIQUE (round_id, investigation_run_id);

      CREATE TABLE investigation_open_hypotheses (
        id UUID PRIMARY KEY,
        investigation_run_id UUID NOT NULL,
        round_id UUID NOT NULL,
        investigation_attempt_id UUID NOT NULL,
        source_call_id UUID NOT NULL,
        version VARCHAR(32) NOT NULL,
        status VARCHAR(32) NOT NULL,
        hypothesis JSONB NOT NULL,
        content_sha256 VARCHAR(64) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT fk_open_hypotheses_round_plan
          FOREIGN KEY (round_id, investigation_run_id)
          REFERENCES investigation_coordination_plans(round_id, investigation_run_id)
          ON DELETE RESTRICT,
        CONSTRAINT fk_open_hypotheses_parent_attempt
          FOREIGN KEY (investigation_attempt_id, investigation_run_id)
          REFERENCES investigation_attempts(id, investigation_run_id) ON DELETE RESTRICT,
        CONSTRAINT fk_open_hypotheses_source_call
          FOREIGN KEY (source_call_id, investigation_run_id)
          REFERENCES investigation_call_reservations(id, investigation_run_id)
          ON DELETE RESTRICT,
        CONSTRAINT uq_open_hypotheses_round_run
          UNIQUE (round_id, investigation_run_id),
        CONSTRAINT uq_open_hypotheses_source_run
          UNIQUE (source_call_id, investigation_run_id),
        CONSTRAINT ck_open_hypotheses_contract CHECK
          (version = 'open-hypothesis-v1' AND status = 'pending_review' AND
           COALESCE(hypothesis ->> 'version', '') = version AND
           COALESCE(hypothesis ->> 'status', '') = status),
        CONSTRAINT ck_open_hypotheses_size CHECK
          (jsonb_typeof(hypothesis) = 'object' AND
           octet_length(hypothesis::text) <= 65536),
        CONSTRAINT ck_open_hypotheses_hash CHECK
          (content_sha256 ~ '^[0-9a-f]{64}$')
      );

      CREATE INDEX ix_open_hypotheses_run_created
        ON investigation_open_hypotheses(investigation_run_id, created_at);

      CREATE FUNCTION reject_open_hypothesis_mutation()
      RETURNS TRIGGER
      LANGUAGE plpgsql
      AS $$
      BEGIN
        RAISE EXCEPTION '动态开放假设不可更新或删除';
      END;
      $$;

      CREATE TRIGGER trg_open_hypotheses_immutable
      BEFORE UPDATE OR DELETE ON investigation_open_hypotheses
      FOR EACH ROW EXECUTE FUNCTION reject_open_hypothesis_mutation();

      ALTER TABLE investigation_call_reservations
        DROP CONSTRAINT ck_call_reservations_loop_payload;

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
             (operation NOT IN ('coordinate', 'synthesize') AND
              COALESCE(model_input ->> 'version', '') =
                'evidence-loop-input-v1'))) OR
           (kind = 'tool' AND tool_request IS NOT NULL)))));

      ALTER TABLE investigation_runs
        DROP CONSTRAINT ck_investigation_runs_dynamic_policy;

      ALTER TABLE investigation_runs
        ADD CONSTRAINT ck_investigation_runs_dynamic_policy CHECK
        ((workflow_version <> 'v3' AND graph_version IS NULL AND
          coordination_policy_version IS NULL) OR
         (workflow_version = 'v3' AND data_source_mode = 'frozen' AND
          COALESCE(graph_version, '') IN
            ('dynamic-investigation-graph-v1', 'dynamic-investigation-graph-v2',
             'dynamic-investigation-graph-v3') AND
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
        IF EXISTS (SELECT 1 FROM investigation_open_hypotheses)
           OR EXISTS (SELECT 1 FROM investigation_runs
                      WHERE graph_version = 'dynamic-investigation-graph-v3') THEN
          RAISE EXCEPTION '不能删除已保存的动态综合图或开放假设';
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
            ('dynamic-investigation-graph-v1', 'dynamic-investigation-graph-v2') AND
          COALESCE(coordination_policy_version, '') = 'dynamic-coordination-v1' AND
          call_budget IS NOT NULL AND model_budget IS NOT NULL AND
          COALESCE(context_policy_version, '') = 'summary-context-v1' AND
          COALESCE(working_summary_policy_version, '') = 'task-working-summary-v1' AND
          COALESCE(evidence_policy_version, '') = 'task-evidence-v1' AND
          prompt_version = 'summary-context-v1' AND
          COALESCE(model_budget #>> '{profile,structured_output_method}', '') =
            'function_calling'));

      ALTER TABLE investigation_call_reservations
        DROP CONSTRAINT ck_call_reservations_loop_payload;

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
             (operation <> 'coordinate' AND
              COALESCE(model_input ->> 'version', '') =
                'evidence-loop-input-v1'))) OR
           (kind = 'tool' AND tool_request IS NOT NULL)))));

      DROP TRIGGER trg_open_hypotheses_immutable
        ON investigation_open_hypotheses;
      DROP FUNCTION reject_open_hypothesis_mutation();
      DROP TABLE investigation_open_hypotheses;
      ALTER TABLE investigation_coordination_plans
        DROP CONSTRAINT uq_coordination_plans_round_run;
    """)
