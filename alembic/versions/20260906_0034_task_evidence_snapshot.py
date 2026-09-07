"""固定新运行的取证策略并保存专业模型调用前的材料。"""

from alembic import op

revision = "0034_task_evidence_snapshot"
down_revision = "0033_model_input_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE investigation_runs ADD COLUMN evidence_policy_version VARCHAR(32);
      ALTER TABLE investigation_runs ADD CONSTRAINT ck_investigation_runs_evidence_policy CHECK
        (evidence_policy_version IS NULL OR (evidence_policy_version = 'task-evidence-v1'
         AND workflow_version = 'v2' AND call_budget IS NOT NULL));
      ALTER TABLE investigation_call_reservations ADD CONSTRAINT uq_call_reservations_evidence_scope
        UNIQUE (id, task_id, task_attempt_id, investigation_run_id);
      CREATE TABLE agent_task_evidence (
        task_id UUID PRIMARY KEY, investigation_run_id UUID NOT NULL, attempt_id UUID NOT NULL,
        call_id UUID NOT NULL, snapshot JSONB NOT NULL, content_sha256 VARCHAR(64) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        CONSTRAINT fk_task_evidence_call FOREIGN KEY
          (call_id, task_id, attempt_id, investigation_run_id) REFERENCES
          investigation_call_reservations (id, task_id, task_attempt_id, investigation_run_id)
          ON DELETE RESTRICT,
        CONSTRAINT ck_task_evidence_size CHECK
          (jsonb_typeof(snapshot) = 'object' AND octet_length(snapshot::text) <= 262144),
        CONSTRAINT ck_task_evidence_hash CHECK (content_sha256 ~ '^[0-9a-f]{64}$')
      );
      ALTER TABLE agent_task_events DROP CONSTRAINT ck_task_events_type;
      ALTER TABLE agent_task_events ADD CONSTRAINT ck_task_events_type CHECK
        (event_type IN ('round.created', 'round.completed', 'round.failed', 'task.started',
         'task.succeeded', 'task.failed', 'task.interrupted', 'task.evidence_saved'));
    """)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_runs WHERE evidence_policy_version IS NOT NULL)
           OR EXISTS (SELECT 1 FROM agent_task_evidence)
           OR EXISTS (SELECT 1 FROM agent_task_events WHERE event_type = 'task.evidence_saved') THEN
          RAISE EXCEPTION '不能删除已固定的取证策略与快照事实';
        END IF;
      END $$;
      DROP TABLE agent_task_evidence;
      ALTER TABLE investigation_call_reservations
        DROP CONSTRAINT uq_call_reservations_evidence_scope;
      ALTER TABLE investigation_runs DROP CONSTRAINT ck_investigation_runs_evidence_policy;
      ALTER TABLE investigation_runs DROP COLUMN evidence_policy_version;
      ALTER TABLE agent_task_events DROP CONSTRAINT ck_task_events_type;
      ALTER TABLE agent_task_events ADD CONSTRAINT ck_task_events_type CHECK
        (event_type IN ('round.created', 'round.completed', 'round.failed', 'task.started',
         'task.succeeded', 'task.failed', 'task.interrupted'));
    """)
