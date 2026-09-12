"""保存运行级协调策略及其创建的不可变轮次计划。"""

from alembic import op

revision = "0041_dynamic_coordination_plans"
down_revision = "0040_summary_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      CREATE TABLE investigation_coordination_policies (
        investigation_run_id UUID PRIMARY KEY
          REFERENCES investigation_runs(id) ON DELETE RESTRICT,
        version VARCHAR(32) NOT NULL,
        policy JSONB NOT NULL,
        content_sha256 VARCHAR(64) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT uq_coordination_policies_run_hash
          UNIQUE (investigation_run_id, content_sha256),
        CONSTRAINT ck_coordination_policies_version CHECK
          (version = 'dynamic-coordination-v1' AND
           COALESCE(policy ->> 'version', '') = version),
        CONSTRAINT ck_coordination_policies_size CHECK
          (jsonb_typeof(policy) = 'object' AND octet_length(policy::text) <= 8192),
        CONSTRAINT ck_coordination_policies_hash CHECK
          (content_sha256 ~ '^[0-9a-f]{64}$')
      );

      CREATE TABLE investigation_coordination_plans (
        round_id UUID PRIMARY KEY,
        investigation_run_id UUID NOT NULL,
        investigation_attempt_id UUID NOT NULL,
        version VARCHAR(40) NOT NULL,
        policy_sha256 VARCHAR(64) NOT NULL,
        plan JSONB NOT NULL,
        content_sha256 VARCHAR(64) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT fk_coordination_plans_round_run
          FOREIGN KEY (round_id, investigation_run_id)
          REFERENCES investigation_rounds(id, investigation_run_id) ON DELETE RESTRICT,
        CONSTRAINT fk_coordination_plans_policy
          FOREIGN KEY (investigation_run_id, policy_sha256)
          REFERENCES investigation_coordination_policies(
            investigation_run_id, content_sha256
          ) ON DELETE RESTRICT,
        CONSTRAINT fk_coordination_plans_parent_attempt
          FOREIGN KEY (investigation_attempt_id, investigation_run_id)
          REFERENCES investigation_attempts(id, investigation_run_id) ON DELETE RESTRICT,
        CONSTRAINT ck_coordination_plans_version CHECK
          (version = 'compiled-coordination-plan-v1' AND
           COALESCE(plan ->> 'version', '') = version AND
           COALESCE(plan ->> 'policy_version', '') = 'dynamic-coordination-v1' AND
           COALESCE(plan #>> '{proposal,version}', '') = 'coordination-proposal-v1'),
        CONSTRAINT ck_coordination_plans_size CHECK
          (jsonb_typeof(plan) = 'object' AND octet_length(plan::text) <= 65536),
        CONSTRAINT ck_coordination_plans_hash CHECK
          (policy_sha256 ~ '^[0-9a-f]{64}$' AND
           content_sha256 ~ '^[0-9a-f]{64}$')
      );

      CREATE INDEX ix_coordination_plans_run_created
        ON investigation_coordination_plans(investigation_run_id, created_at);

      CREATE FUNCTION reject_coordination_fact_mutation()
      RETURNS TRIGGER
      LANGUAGE plpgsql
      AS $$
      BEGIN
        RAISE EXCEPTION '动态协调策略与计划不可更新或删除';
      END;
      $$;

      CREATE TRIGGER trg_coordination_policies_immutable
      BEFORE UPDATE OR DELETE ON investigation_coordination_policies
      FOR EACH ROW EXECUTE FUNCTION reject_coordination_fact_mutation();

      CREATE TRIGGER trg_coordination_plans_immutable
      BEFORE UPDATE OR DELETE ON investigation_coordination_plans
      FOR EACH ROW EXECUTE FUNCTION reject_coordination_fact_mutation();
    """)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_coordination_policies)
           OR EXISTS (SELECT 1 FROM investigation_coordination_plans) THEN
          RAISE EXCEPTION '不能删除已保存的动态协调策略或计划';
        END IF;
      END $$;

      DROP TRIGGER trg_coordination_plans_immutable
        ON investigation_coordination_plans;
      DROP TRIGGER trg_coordination_policies_immutable
        ON investigation_coordination_policies;
      DROP FUNCTION reject_coordination_fact_mutation();
      DROP TABLE investigation_coordination_plans;
      DROP TABLE investigation_coordination_policies;
    """)
