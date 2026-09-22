"""保存确定性开放假设审查路由事实。"""

from alembic import op

revision = "0047_review_routing"
down_revision = "0046_dynamic_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE investigation_hypothesis_reviews
        ADD CONSTRAINT uq_hypothesis_reviews_route_source
        UNIQUE (id, open_hypothesis_id, round_id, investigation_run_id);

      CREATE TABLE investigation_hypothesis_review_routes (
        id UUID PRIMARY KEY,
        investigation_run_id UUID NOT NULL,
        round_id UUID NOT NULL,
        open_hypothesis_id UUID NOT NULL,
        hypothesis_review_id UUID NOT NULL,
        investigation_attempt_id UUID NOT NULL,
        version VARCHAR(40) NOT NULL,
        status VARCHAR(32) NOT NULL,
        decision JSONB NOT NULL,
        review_sha256 VARCHAR(64) NOT NULL,
        policy_sha256 VARCHAR(64) NOT NULL,
        content_sha256 VARCHAR(64) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT fk_hypothesis_review_routes_review
          FOREIGN KEY (
            hypothesis_review_id, open_hypothesis_id, round_id, investigation_run_id
          ) REFERENCES investigation_hypothesis_reviews(
            id, open_hypothesis_id, round_id, investigation_run_id
          ) ON DELETE RESTRICT,
        CONSTRAINT fk_hypothesis_review_routes_parent
          FOREIGN KEY (investigation_attempt_id, investigation_run_id)
          REFERENCES investigation_attempts(id, investigation_run_id) ON DELETE RESTRICT,
        CONSTRAINT fk_hypothesis_review_routes_policy
          FOREIGN KEY (investigation_run_id, policy_sha256)
          REFERENCES investigation_coordination_policies(
            investigation_run_id, content_sha256
          ) ON DELETE RESTRICT,
        CONSTRAINT uq_hypothesis_review_routes_review_run
          UNIQUE (hypothesis_review_id, investigation_run_id),
        CONSTRAINT uq_hypothesis_review_routes_round_run
          UNIQUE (round_id, investigation_run_id),
        CONSTRAINT ck_hypothesis_review_routes_contract CHECK
          (version = 'review-routing-v1' AND status = 'recorded' AND
           COALESCE(decision ->> 'version', '') = version AND
           COALESCE(decision ->> 'status', '') = status AND
           COALESCE(decision ->> 'rule_version', '') =
             'deterministic-review-routing-v1' AND
           COALESCE(decision ->> 'route', '') IN
             ('follow_up', 'await_human', 'candidate_evaluation')),
        CONSTRAINT ck_hypothesis_review_routes_size CHECK
          (jsonb_typeof(decision) = 'object' AND
           octet_length(decision::text) <= 32768),
        CONSTRAINT ck_hypothesis_review_routes_hash CHECK
          (review_sha256 ~ '^[0-9a-f]{64}$' AND
           policy_sha256 ~ '^[0-9a-f]{64}$' AND
           content_sha256 ~ '^[0-9a-f]{64}$')
      );

      CREATE INDEX ix_hypothesis_review_routes_run_created
        ON investigation_hypothesis_review_routes(investigation_run_id, created_at);

      CREATE FUNCTION reject_hypothesis_review_route_mutation()
      RETURNS TRIGGER
      LANGUAGE plpgsql
      AS $$
      BEGIN
        RAISE EXCEPTION '开放假设审查路由事实不可更新或删除';
      END;
      $$;

      CREATE TRIGGER trg_hypothesis_review_routes_immutable
      BEFORE UPDATE OR DELETE ON investigation_hypothesis_review_routes
      FOR EACH ROW EXECUTE FUNCTION reject_hypothesis_review_route_mutation();

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
    """)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_hypothesis_review_routes)
           OR EXISTS (SELECT 1 FROM investigation_runs
                      WHERE graph_version = 'dynamic-investigation-graph-v5') THEN
          RAISE EXCEPTION '不能删除已保存的确定性审查路由图或路由事实';
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
             'dynamic-investigation-graph-v3', 'dynamic-investigation-graph-v4') AND
          COALESCE(coordination_policy_version, '') = 'dynamic-coordination-v1' AND
          call_budget IS NOT NULL AND model_budget IS NOT NULL AND
          COALESCE(context_policy_version, '') = 'summary-context-v1' AND
          COALESCE(working_summary_policy_version, '') = 'task-working-summary-v1' AND
          COALESCE(evidence_policy_version, '') = 'task-evidence-v1' AND
          prompt_version = 'summary-context-v1' AND
          COALESCE(model_budget #>> '{profile,structured_output_method}', '') =
            'function_calling'));

      DROP TRIGGER trg_hypothesis_review_routes_immutable
        ON investigation_hypothesis_review_routes;
      DROP FUNCTION reject_hypothesis_review_route_mutation();
      DROP TABLE investigation_hypothesis_review_routes;
      ALTER TABLE investigation_hypothesis_reviews
        DROP CONSTRAINT uq_hypothesis_reviews_route_source;
    """)
