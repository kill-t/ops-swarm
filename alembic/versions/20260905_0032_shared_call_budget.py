"""运行级调用预算与预留账本。"""

from alembic import op

revision = "0032_shared_call_budget"
down_revision = "0031_fixed_graph_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
      ALTER TABLE investigation_runs ADD COLUMN call_budget JSONB;
      ALTER TABLE investigation_runs ADD CONSTRAINT ck_investigation_runs_call_budget CHECK
        (call_budget IS NULL OR (workflow_version = 'v2' AND jsonb_typeof(call_budget) = 'object'
          AND octet_length(call_budget::text) <= 8192));
      CREATE TABLE investigation_call_reservations (
        id UUID PRIMARY KEY, investigation_run_id UUID NOT NULL,
        investigation_attempt_id UUID NOT NULL, task_id UUID, task_attempt_id UUID,
        sequence INTEGER NOT NULL, idempotency_key VARCHAR(120) NOT NULL,
        operation VARCHAR(32) NOT NULL, kind VARCHAR(8) NOT NULL, status VARCHAR(16) NOT NULL,
        error_code VARCHAR(100), reserved_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ,
        CONSTRAINT fk_call_reservations_parent FOREIGN KEY
          (investigation_attempt_id, investigation_run_id)
          REFERENCES investigation_attempts(id, investigation_run_id) ON DELETE RESTRICT,
        CONSTRAINT fk_call_reservations_task FOREIGN KEY
          (task_attempt_id, task_id, investigation_run_id)
          REFERENCES agent_task_attempts(id, task_id, investigation_run_id) ON DELETE RESTRICT,
        CONSTRAINT uq_call_reservations_sequence UNIQUE (investigation_run_id, sequence),
        CONSTRAINT uq_call_reservations_key UNIQUE (investigation_run_id, idempotency_key),
        CONSTRAINT ck_call_reservations_sequence CHECK (sequence BETWEEN 1 AND 160),
        CONSTRAINT ck_call_reservations_status CHECK
          (status IN ('reserved', 'succeeded', 'failed', 'unknown')),
        CONSTRAINT ck_call_reservations_time CHECK
          ((status = 'reserved') = (finished_at IS NULL) AND finished_at >= reserved_at),
        CONSTRAINT ck_call_reservations_error CHECK
          ((status IN ('failed', 'unknown')) = (error_code IS NOT NULL)),
        CONSTRAINT ck_call_reservations_operation CHECK
          ((operation IN ('query_logs', 'query_metrics', 'list_changes') AND kind = 'tool') OR
           (operation IN ('analyze_log', 'analyze_metric', 'analyze_change', 'synthesize')
             AND kind = 'model')),
        CONSTRAINT ck_call_reservations_scope CHECK
          ((operation = 'synthesize' AND task_id IS NULL AND task_attempt_id IS NULL) OR
           (operation <> 'synthesize' AND task_id IS NOT NULL AND task_attempt_id IS NOT NULL))
      );
    """)


def downgrade() -> None:
    op.execute("""
      DROP TABLE investigation_call_reservations;
      ALTER TABLE investigation_runs DROP CONSTRAINT ck_investigation_runs_call_budget;
      ALTER TABLE investigation_runs DROP COLUMN call_budget;
    """)
