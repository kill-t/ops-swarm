"""持久保存动态图终端交接并原子释放父任务租约。"""

from alembic import op

revision = "0067_terminal_handoff"
down_revision = "0066_coordination_experience"
branch_labels = None
depends_on = None


def _replace_lifecycle_checks(*, enabled: bool) -> None:
    run_states = "'queued', 'running', 'succeeded', 'failed'"
    active_states = "'queued', 'running'"
    attempt_states = "'running', 'succeeded', 'failed', 'lease_expired'"
    success_states = "'succeeded'"
    event_types = (
        "'round.created', 'round.completed', 'round.failed', 'task.started', "
        "'task.succeeded', 'task.failed', 'task.interrupted', 'task.evidence_saved'"
    )
    if enabled:
        run_states += ", 'await_human', 'await_candidate'"
        active_states += ", 'await_human', 'await_candidate'"
        attempt_states += ", 'handed_off'"
        success_states += ", 'handed_off'"
        event_types += ", 'run.handed_off'"
    op.execute(f"""
      ALTER TABLE investigation_runs
        DROP CONSTRAINT ck_investigation_runs_status,
        DROP CONSTRAINT ck_investigation_runs_completion_shape;
      ALTER TABLE investigation_runs
        ADD CONSTRAINT ck_investigation_runs_status CHECK (status IN ({run_states})),
        ADD CONSTRAINT ck_investigation_runs_completion_shape CHECK
          ((status IN ({active_states}) AND completed_at IS NULL) OR
           (status IN ('succeeded', 'failed') AND completed_at IS NOT NULL));
      DROP INDEX uq_investigation_runs_active_incident;
      CREATE UNIQUE INDEX uq_investigation_runs_active_incident
        ON investigation_runs(incident_id) WHERE status IN ({active_states});
      ALTER TABLE investigation_attempts
        DROP CONSTRAINT ck_investigation_attempts_status,
        DROP CONSTRAINT ck_investigation_attempts_terminal_shape;
      ALTER TABLE investigation_attempts
        ADD CONSTRAINT ck_investigation_attempts_status CHECK (status IN ({attempt_states})),
        ADD CONSTRAINT ck_investigation_attempts_terminal_shape CHECK
          ((status = 'running' AND finished_at IS NULL AND duration_ms IS NULL
            AND will_retry IS NULL AND error_code IS NULL AND error_message IS NULL
            AND estimated_cost_usd IS NULL) OR
           (status IN ({success_states}) AND finished_at IS NOT NULL AND duration_ms >= 0
            AND will_retry = false AND error_code IS NULL AND error_message IS NULL) OR
           (status IN ('failed', 'lease_expired') AND finished_at IS NOT NULL
            AND duration_ms >= 0 AND will_retry IS NOT NULL
            AND error_code IS NOT NULL AND error_message IS NOT NULL));
      ALTER TABLE agent_task_events DROP CONSTRAINT ck_task_events_type;
      ALTER TABLE agent_task_events ADD CONSTRAINT ck_task_events_type
        CHECK (event_type IN ({event_types}));
    """)


def upgrade() -> None:
    _replace_lifecycle_checks(enabled=True)
    op.execute("""
      ALTER TABLE investigation_runs
        ADD CONSTRAINT ck_investigation_runs_handoff_scope CHECK
          (status NOT IN ('await_human', 'await_candidate') OR
           (workflow_version = 'v3' AND graph_version = 'dynamic-investigation-graph-v15'
            AND data_source_mode = 'frozen')),
        ADD CONSTRAINT ck_investigation_runs_v3_no_success CHECK
          (workflow_version <> 'v3' OR status <> 'succeeded');

      CREATE TABLE investigation_terminal_handoffs (
        id UUID PRIMARY KEY,
        investigation_run_id UUID NOT NULL,
        investigation_attempt_id UUID NOT NULL,
        source_review_route_id UUID NOT NULL,
        source_sha256 VARCHAR(64) NOT NULL,
        version VARCHAR(32) NOT NULL,
        destination VARCHAR(32) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT uq_terminal_handoffs_run UNIQUE (investigation_run_id),
        CONSTRAINT fk_terminal_handoffs_route
          FOREIGN KEY (source_review_route_id, investigation_run_id)
          REFERENCES investigation_hypothesis_review_routes(id, investigation_run_id)
          ON DELETE RESTRICT,
        CONSTRAINT fk_terminal_handoffs_attempt
          FOREIGN KEY (investigation_attempt_id, investigation_run_id)
          REFERENCES investigation_attempts(id, investigation_run_id) ON DELETE RESTRICT,
        CONSTRAINT ck_terminal_handoffs_version CHECK (version = 'terminal-handoff-v1'),
        CONSTRAINT ck_terminal_handoffs_destination
          CHECK (destination IN ('await_human', 'candidate_evaluation')),
        CONSTRAINT ck_terminal_handoffs_hash CHECK (source_sha256 ~ '^[0-9a-f]{64}$')
      );

      CREATE FUNCTION reject_terminal_handoff_mutation() RETURNS trigger AS $$
      BEGIN
        RAISE EXCEPTION '终端交接事实不可更新或删除';
      END;
      $$ LANGUAGE plpgsql;
      CREATE TRIGGER terminal_handoffs_immutable
        BEFORE UPDATE OR DELETE ON investigation_terminal_handoffs
        FOR EACH ROW EXECUTE FUNCTION reject_terminal_handoff_mutation();

      CREATE FUNCTION validate_terminal_handoff_insert() RETURNS trigger AS $$
      DECLARE
        parent investigation_attempts%ROWTYPE;
        work work_items%ROWTYPE;
        run investigation_runs%ROWTYPE;
        route investigation_hypothesis_review_routes%ROWTYPE;
        source_round investigation_rounds%ROWTYPE;
      BEGIN
        SELECT * INTO work FROM work_items
          WHERE investigation_run_id = NEW.investigation_run_id FOR UPDATE;
        IF NOT FOUND THEN
          RAISE EXCEPTION '终端交接缺少父任务';
        END IF;
        SELECT * INTO run FROM investigation_runs WHERE id = NEW.investigation_run_id;
        SELECT * INTO parent FROM investigation_attempts
          WHERE id = NEW.investigation_attempt_id
            AND investigation_run_id = NEW.investigation_run_id;
        IF run.id IS NULL OR parent.id IS NULL OR run.status <> 'running'
           OR run.workflow_version <> 'v3'
           OR run.graph_version IS DISTINCT FROM 'dynamic-investigation-graph-v15'
           OR run.data_source_mode <> 'frozen' OR run.mode <> 'multi_agent'
           OR parent.status <> 'running' OR work.status <> 'running'
           OR parent.work_item_id IS DISTINCT FROM work.id
           OR work.lease_token IS DISTINCT FROM parent.lease_token
           OR work.locked_until IS NULL OR work.locked_until <= NEW.created_at
           OR parent.lease_expires_at <= NEW.created_at
           OR parent.started_at > NEW.created_at THEN
          RAISE EXCEPTION '终端交接需要当前有效父租约与运行';
        END IF;
        PERFORM 1 FROM incidents WHERE id = run.incident_id AND status = 'investigating'
          FOR UPDATE;
        IF NOT FOUND THEN
          RAISE EXCEPTION '终端交接需要仍可调查的事故';
        END IF;
        SELECT * INTO route FROM investigation_hypothesis_review_routes
          WHERE id = NEW.source_review_route_id
            AND investigation_run_id = NEW.investigation_run_id;
        IF NOT FOUND OR route.content_sha256 IS DISTINCT FROM NEW.source_sha256
           OR (route.decision ->> 'route') IS DISTINCT FROM NEW.destination
           OR NEW.destination NOT IN ('await_human', 'candidate_evaluation')
           OR route.created_at > NEW.created_at THEN
          RAISE EXCEPTION '终端交接来源路由不匹配';
        END IF;
        SELECT * INTO source_round FROM investigation_rounds
          WHERE id = route.round_id AND investigation_run_id = NEW.investigation_run_id;
        IF NOT FOUND OR source_round.status <> 'completed'
           OR source_round.completed_at IS NULL
           OR source_round.completed_at > route.created_at
           OR EXISTS (SELECT 1 FROM investigation_rounds
                      WHERE investigation_run_id = NEW.investigation_run_id
                        AND (round_number > source_round.round_number OR status <> 'completed'))
           OR EXISTS (SELECT 1 FROM agent_tasks
                      WHERE investigation_run_id = NEW.investigation_run_id
                        AND status <> 'succeeded')
           OR EXISTS (SELECT 1 FROM agent_task_attempts
                      WHERE investigation_run_id = NEW.investigation_run_id AND status = 'running')
           OR EXISTS (SELECT 1 FROM investigation_call_reservations
                      WHERE investigation_run_id = NEW.investigation_run_id AND status = 'reserved')
        THEN
          RAISE EXCEPTION '终端交接必须来自最高已完成轮次且无未结算工作';
        END IF;
        RETURN NEW;
      END;
      $$ LANGUAGE plpgsql;
      CREATE TRIGGER terminal_handoffs_source
        BEFORE INSERT ON investigation_terminal_handoffs
        FOR EACH ROW EXECUTE FUNCTION validate_terminal_handoff_insert();

      CREATE FUNCTION validate_terminal_handoff_state() RETURNS trigger AS $$
      DECLARE
        run_id UUID;
        run investigation_runs%ROWTYPE;
        handoff investigation_terminal_handoffs%ROWTYPE;
        parent investigation_attempts%ROWTYPE;
        work work_items%ROWTYPE;
        expected_status TEXT;
      BEGIN
        IF TG_TABLE_NAME = 'investigation_runs' THEN
          run_id := NEW.id;
        ELSE
          run_id := NEW.investigation_run_id;
        END IF;
        SELECT * INTO run FROM investigation_runs WHERE id = run_id;
        SELECT * INTO handoff FROM investigation_terminal_handoffs
          WHERE investigation_run_id = run_id;
        IF handoff.id IS NULL THEN
          IF run.status IN ('await_human', 'await_candidate') OR EXISTS
             (SELECT 1 FROM investigation_attempts
              WHERE investigation_run_id = run_id AND status = 'handed_off') THEN
            RAISE EXCEPTION '等待状态或交接尝试缺少终端交接事实';
          END IF;
          RETURN NULL;
        END IF;
        SELECT * INTO parent FROM investigation_attempts
          WHERE id = handoff.investigation_attempt_id;
        SELECT * INTO work FROM work_items WHERE investigation_run_id = run_id;
        expected_status := CASE handoff.destination
          WHEN 'await_human' THEN 'await_human' ELSE 'await_candidate' END;
        IF run.status IS DISTINCT FROM expected_status OR run.completed_at IS NOT NULL
           OR run.result IS NOT NULL OR run.metrics IS NOT NULL
           OR work.id IS NULL OR parent.id IS NULL OR work.status <> 'completed'
           OR work.completed_at IS DISTINCT FROM handoff.created_at
           OR work.locked_by IS NOT NULL OR work.locked_until IS NOT NULL
           OR work.lease_token IS NOT NULL OR parent.status <> 'handed_off'
           OR parent.work_item_id IS DISTINCT FROM work.id
           OR parent.finished_at IS DISTINCT FROM handoff.created_at
           OR EXISTS (SELECT 1 FROM investigation_attempts
                      WHERE investigation_run_id = run_id AND status = 'handed_off'
                        AND id <> handoff.investigation_attempt_id) THEN
          RAISE EXCEPTION '终端交接与运行、任务、父尝试状态不一致';
        END IF;
        RETURN NULL;
      END;
      $$ LANGUAGE plpgsql;
      CREATE CONSTRAINT TRIGGER terminal_handoffs_state
        AFTER INSERT ON investigation_terminal_handoffs DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION validate_terminal_handoff_state();
      CREATE CONSTRAINT TRIGGER investigation_runs_handoff_state
        AFTER INSERT OR UPDATE ON investigation_runs DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION validate_terminal_handoff_state();
      CREATE CONSTRAINT TRIGGER work_items_handoff_state
        AFTER INSERT OR UPDATE ON work_items DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION validate_terminal_handoff_state();
      CREATE CONSTRAINT TRIGGER investigation_attempts_handoff_state
        AFTER INSERT OR UPDATE ON investigation_attempts DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION validate_terminal_handoff_state();
    """)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_terminal_handoffs)
           OR EXISTS (SELECT 1 FROM investigation_runs
                      WHERE status IN ('await_human', 'await_candidate'))
           OR EXISTS (SELECT 1 FROM investigation_attempts WHERE status = 'handed_off')
           OR EXISTS (SELECT 1 FROM agent_task_events WHERE event_type = 'run.handed_off') THEN
          RAISE EXCEPTION '不能删除已保存的终端交接事实或等待状态';
        END IF;
      END $$;
      DROP TRIGGER investigation_attempts_handoff_state ON investigation_attempts;
      DROP TRIGGER work_items_handoff_state ON work_items;
      DROP TRIGGER investigation_runs_handoff_state ON investigation_runs;
      DROP TRIGGER terminal_handoffs_state ON investigation_terminal_handoffs;
      DROP FUNCTION validate_terminal_handoff_state();
      DROP TRIGGER terminal_handoffs_source ON investigation_terminal_handoffs;
      DROP FUNCTION validate_terminal_handoff_insert();
      DROP TRIGGER terminal_handoffs_immutable ON investigation_terminal_handoffs;
      DROP FUNCTION reject_terminal_handoff_mutation();
      DROP TABLE investigation_terminal_handoffs;
      ALTER TABLE investigation_runs
        DROP CONSTRAINT ck_investigation_runs_handoff_scope,
        DROP CONSTRAINT ck_investigation_runs_v3_no_success;
    """)
    _replace_lifecycle_checks(enabled=False)
