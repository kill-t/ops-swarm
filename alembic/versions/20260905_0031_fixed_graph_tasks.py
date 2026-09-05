"""固定图版本、专业结果与进度事件。"""

from alembic import op

revision = "0031_fixed_graph_tasks"
down_revision = "0030_investigation_task_facts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE investigation_runs
          ADD COLUMN workflow_version VARCHAR(16) NOT NULL DEFAULT 'v1';
        ALTER TABLE investigation_runs ADD CONSTRAINT ck_investigation_runs_workflow_version
          CHECK (workflow_version IN ('v1', 'v2') AND
            (workflow_version = 'v1' OR (mode = 'multi_agent' AND data_source_mode = 'frozen')));
        ALTER TABLE agent_task_attempts ADD CONSTRAINT uq_task_attempts_identity
          UNIQUE (id, task_id, investigation_run_id);
        CREATE TABLE agent_task_results (
          task_id UUID PRIMARY KEY, investigation_run_id UUID NOT NULL,
          attempt_id UUID NOT NULL, result JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT fk_task_results_attempt FOREIGN KEY (attempt_id, task_id, investigation_run_id)
            REFERENCES agent_task_attempts(id, task_id, investigation_run_id) ON DELETE RESTRICT,
          CONSTRAINT ck_task_results_size CHECK
            (jsonb_typeof(result) = 'object' AND octet_length(result::text) <= 262144)
        );
        CREATE TABLE agent_task_events (
          investigation_run_id UUID NOT NULL REFERENCES investigation_runs(id) ON DELETE RESTRICT,
          cursor INTEGER NOT NULL, event_type VARCHAR(32) NOT NULL, task_id UUID,
          occurred_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (investigation_run_id, cursor),
          CONSTRAINT fk_task_events_task FOREIGN KEY (task_id, investigation_run_id)
            REFERENCES agent_tasks(id, investigation_run_id) ON DELETE RESTRICT,
          CONSTRAINT ck_task_events_cursor CHECK (cursor >= 1),
          CONSTRAINT ck_task_events_type CHECK (event_type IN
            ('round.created', 'round.completed', 'round.failed', 'task.started',
             'task.succeeded', 'task.failed', 'task.interrupted'))
        );
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE agent_task_events;
        DROP TABLE agent_task_results;
        ALTER TABLE agent_task_attempts DROP CONSTRAINT uq_task_attempts_identity;
        ALTER TABLE investigation_runs DROP CONSTRAINT ck_investigation_runs_workflow_version;
        ALTER TABLE investigation_runs DROP COLUMN workflow_version;
    """)
