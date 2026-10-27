"""候选恢复结论、进度事件及父运行终态的不可变同事务收尾。"""

from alembic import op

revision = "0084_candidate_settlement"
down_revision = "0083_candidate_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _lifecycle(settled=True)
    op.execute(_TABLE)
    op.execute(_SOURCE)
    op.execute(_GUARDS)
    op.execute(_STATE)
    op.execute(_HANDOFF_STATE)


def _lifecycle(*, settled: bool) -> None:
    terminal = ",'settled'" if settled else ""
    event = ",'run.candidate_settled'" if settled else ""
    op.execute(f"""
      ALTER TABLE investigation_runs DROP CONSTRAINT ck_investigation_runs_status,
        DROP CONSTRAINT ck_investigation_runs_completion_shape;
      ALTER TABLE investigation_runs ADD CONSTRAINT ck_investigation_runs_status CHECK
        (status IN ('queued','running','succeeded','failed','await_human','await_candidate',
          'cancelled'{terminal})),
        ADD CONSTRAINT ck_investigation_runs_completion_shape CHECK
        ((status IN ('queued','running','await_human','await_candidate') AND completed_at IS NULL)
         OR (status IN ('succeeded','failed','cancelled'{terminal}) AND completed_at IS NOT NULL));
      DROP INDEX uq_investigation_runs_active_incident;
      CREATE UNIQUE INDEX uq_investigation_runs_active_incident ON investigation_runs(incident_id)
        WHERE status IN ('queued','running','await_human','await_candidate'{terminal});
      ALTER TABLE agent_task_events DROP CONSTRAINT ck_task_events_type;
      ALTER TABLE agent_task_events ADD CONSTRAINT ck_task_events_type CHECK
        (event_type IN ('round.created','round.completed','round.failed','task.started',
          'task.succeeded','task.failed','task.interrupted','task.evidence_saved',
          'run.handed_off','run.human_input_saved','run.cancelled'{event}));
    """)
    if settled:
        op.execute("""
          ALTER TABLE investigation_runs ADD CONSTRAINT ck_investigation_runs_settled_scope CHECK
            (status<>'settled' OR (workflow_version='v3'
              AND graph_version='dynamic-investigation-graph-v15' AND data_source_mode='frozen'
              AND result IS NULL AND metrics IS NULL AND last_error IS NULL));
        """)


_TABLE = """
CREATE TABLE investigation_candidate_settlements (
 id UUID PRIMARY KEY,
 investigation_run_id UUID NOT NULL UNIQUE REFERENCES investigation_runs(id) ON DELETE RESTRICT,
 recovery_job_id UUID NOT NULL UNIQUE REFERENCES candidate_recovery_jobs(id) ON DELETE RESTRICT,
 candidate_job_id UUID NOT NULL REFERENCES investigation_candidate_evaluation_jobs(id)
   ON DELETE RESTRICT,
 verification_version INTEGER NOT NULL,
 operation_id UUID NOT NULL,
 execution_result_sha256 VARCHAR(64) NOT NULL,
 recovery_result_sha256 VARCHAR(64) NOT NULL,
 handoff_id UUID NOT NULL REFERENCES investigation_terminal_handoffs(id) ON DELETE RESTRICT,
 previous_run_status VARCHAR(16) NOT NULL,
 previous_run_updated_at TIMESTAMPTZ NOT NULL,
 incident_version INTEGER NOT NULL,
 outcome VARCHAR(32) NOT NULL,
 disposition VARCHAR(40) NOT NULL,
 reason_code VARCHAR(100) NOT NULL,
 event_cursor INTEGER NOT NULL,
 policy_version VARCHAR(40) NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 content_sha256 VARCHAR(64) NOT NULL,
 CONSTRAINT fk_candidate_settlement_event FOREIGN KEY (investigation_run_id,event_cursor)
   REFERENCES agent_task_events(investigation_run_id,cursor) ON DELETE RESTRICT
   DEFERRABLE INITIALLY DEFERRED,
 CONSTRAINT ck_candidate_settlement_versions CHECK
   (verification_version BETWEEN 1 AND 8 AND incident_version>=2 AND event_cursor>=1),
 CONSTRAINT ck_candidate_settlement_previous_status CHECK
   (previous_run_status IN ('await_candidate','await_human')),
 CONSTRAINT ck_candidate_settlement_outcome CHECK
   (outcome IN ('recovered','not_recovered','source_failed','not_executed','execution_unknown')),
 CONSTRAINT ck_candidate_settlement_disposition CHECK
   (disposition IN ('recovery_verified','reinvestigation_required','human_resolution_required')),
 CONSTRAINT ck_candidate_settlement_hashes CHECK
   (execution_result_sha256 ~ '^[0-9a-f]{64}$' AND recovery_result_sha256 ~ '^[0-9a-f]{64}$'
     AND content_sha256 ~ '^[0-9a-f]{64}$'),
 CONSTRAINT ck_candidate_settlement_policy CHECK (policy_version='candidate-settlement-v1'),
 CONSTRAINT ck_candidate_settlement_reason CHECK (reason_code ~ '^[a-z][a-z0-9_]{0,99}$')
);
"""


_SOURCE = """
CREATE FUNCTION candidate_settlement_content(item investigation_candidate_settlements)
 RETURNS JSONB AS $$
 SELECT jsonb_build_object(
   'id',item.id::text,'investigation_run_id',item.investigation_run_id::text,
   'recovery_job_id',item.recovery_job_id::text,'candidate_job_id',item.candidate_job_id::text,
   'verification_version',item.verification_version,'operation_id',item.operation_id::text,
   'execution_result_sha256',item.execution_result_sha256,
   'recovery_result_sha256',item.recovery_result_sha256,'handoff_id',item.handoff_id::text,
   'previous_run_status',item.previous_run_status,
   'previous_run_updated_at',candidate_recovery_utc_text(item.previous_run_updated_at),
   'incident_version',item.incident_version,'outcome',item.outcome,
   'disposition',item.disposition,'reason_code',item.reason_code,'event_cursor',item.event_cursor,
   'policy_version',item.policy_version,'created_at',candidate_recovery_utc_text(item.created_at));
$$ LANGUAGE SQL IMMUTABLE STRICT;

-- 窄校验不回调候选来源，避免交接→收尾→恢复→候选→交接递归。
CREATE FUNCTION assert_candidate_settlement_leaf(item investigation_candidate_settlements)
 RETURNS VOID AS $$
DECLARE run investigation_runs%ROWTYPE; recovery candidate_recovery_jobs%ROWTYPE;
 result candidate_recovery_results%ROWTYPE; execution candidate_release_execution_results%ROWTYPE;
 handoff investigation_terminal_handoffs%ROWTYPE; event agent_task_events%ROWTYPE;
BEGIN
 SELECT * INTO run FROM investigation_runs WHERE id=item.investigation_run_id;
 SELECT * INTO recovery FROM candidate_recovery_jobs WHERE id=item.recovery_job_id;
 SELECT * INTO result FROM candidate_recovery_results WHERE job_id=recovery.id;
 SELECT * INTO execution FROM candidate_release_execution_results WHERE job_id=recovery.id;
 SELECT * INTO handoff FROM investigation_terminal_handoffs WHERE id=item.handoff_id;
 SELECT * INTO event FROM agent_task_events WHERE investigation_run_id=run.id
   AND cursor=item.event_cursor;
 IF (item.id=item.investigation_run_id AND recovery.status='completed'
   AND recovery.investigation_run_id=run.id AND recovery.candidate_job_id=item.candidate_job_id
   AND recovery.verification_version=item.verification_version
   AND recovery.operation_id=item.operation_id
   AND recovery.execution_result_sha256=item.execution_result_sha256
   AND execution.content_sha256=item.execution_result_sha256
   AND execution.content_sha256=encode(sha256(convert_to(
     candidate_canonical_json(execution.content),'UTF8')),'hex')
   AND result.content_sha256=item.recovery_result_sha256
   AND result.content_sha256=encode(sha256(convert_to(
     candidate_canonical_json(result.content),'UTF8')),'hex')
   AND result.content->>'outcome'=item.outcome AND result.content->>'reason_code'=item.reason_code
   AND result.content->>'execution_job_id'=recovery.id::text
   AND result.content->>'operation_id'=item.operation_id::text
   AND result.content->>'execution_result_sha256'=item.execution_result_sha256
   AND result.content->'parent_advanced'='false'::jsonb
   AND result.created_at=recovery.completed_at AND result.created_at<=item.created_at
   AND handoff.investigation_run_id=run.id
   AND item.previous_run_status=CASE handoff.destination WHEN 'await_human' THEN 'await_human'
     ELSE 'await_candidate' END
   AND item.previous_run_updated_at>=handoff.created_at
   AND isfinite(item.previous_run_updated_at) AND isfinite(item.created_at)
   AND item.previous_run_updated_at<=item.created_at
   AND item.disposition=CASE WHEN item.outcome='recovered' THEN 'recovery_verified'
     WHEN execution.content->>'outcome'='applied_current' AND item.outcome='not_recovered'
       AND item.reason_code='business_probe_failed' THEN 'reinvestigation_required'
     ELSE 'human_resolution_required' END
   AND item.content_sha256=encode(sha256(convert_to(
     candidate_canonical_json(candidate_settlement_content(item)),'UTF8')),'hex')
   AND run.status='settled' AND run.completed_at=item.created_at AND run.updated_at=item.created_at
   AND run.result IS NULL AND run.metrics IS NULL AND run.last_error IS NULL
   AND event.event_type='run.candidate_settled' AND event.task_id IS NULL
   AND event.occurred_at=item.created_at
   AND (SELECT count(*) FROM agent_task_events WHERE investigation_run_id=run.id
     AND event_type='run.candidate_settled')=1) IS NOT TRUE THEN
   RAISE EXCEPTION '候选收尾缺少精确来源、摘要、父终态或进度事件闭环'; END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION assert_candidate_settlement_source(item investigation_candidate_settlements)
 RETURNS VOID AS $$
DECLARE recovery candidate_recovery_jobs%ROWTYPE; original candidate_release_execution_jobs%ROWTYPE;
 result candidate_recovery_results%ROWTYPE; request candidate_release_execution_requests%ROWTYPE;
 candidate investigation_candidate_evaluation_jobs%ROWTYPE;
 handoff investigation_terminal_handoffs%ROWTYPE;
BEGIN
 SELECT * INTO recovery FROM candidate_recovery_jobs WHERE id=item.recovery_job_id;
 IF recovery.id IS NULL OR recovery.status<>'completed' THEN
   RAISE EXCEPTION '候选收尾缺少终态恢复验证来源'; END IF;
 PERFORM assert_recovery_history(recovery);
 SELECT * INTO original FROM candidate_release_execution_jobs WHERE id=recovery.id;
 PERFORM assert_release_job_history(original);
 SELECT * INTO result FROM candidate_recovery_results WHERE job_id=recovery.id;
 SELECT * INTO request FROM candidate_release_execution_requests WHERE id=original.request_id;
 SELECT * INTO candidate FROM investigation_candidate_evaluation_jobs
   WHERE id=recovery.candidate_job_id;
 SELECT * INTO handoff FROM investigation_terminal_handoffs
   WHERE investigation_run_id=item.investigation_run_id;
 IF (item.id=item.investigation_run_id
   AND item.investigation_run_id=recovery.investigation_run_id
   AND item.candidate_job_id=recovery.candidate_job_id
   AND item.verification_version=recovery.verification_version
   AND item.operation_id=recovery.operation_id
   AND item.execution_result_sha256=recovery.execution_result_sha256
   AND item.recovery_result_sha256=result.content_sha256
   AND item.handoff_id=handoff.id
   AND candidate.investigation_run_id=item.investigation_run_id
   AND item.previous_run_status=CASE candidate.source_kind WHEN 'terminal_handoff'
     THEN 'await_candidate' ELSE 'await_human' END
   AND item.previous_run_updated_at>=handoff.created_at
   AND isfinite(item.previous_run_updated_at) AND isfinite(item.created_at)
   AND item.created_at>=item.previous_run_updated_at AND item.created_at>=result.created_at
   AND item.incident_version=request.incident_version
   AND item.outcome=result.content->>'outcome' AND item.reason_code=result.content->>'reason_code'
   AND item.disposition=CASE WHEN item.outcome='recovered' THEN 'recovery_verified'
     WHEN assert_recovery_source(recovery)='applied_current' AND item.outcome='not_recovered'
       AND item.reason_code='business_probe_failed' THEN 'reinvestigation_required'
     ELSE 'human_resolution_required' END
   AND item.content_sha256=encode(sha256(convert_to(
     candidate_canonical_json(candidate_settlement_content(item)),'UTF8')),'hex')) IS NOT TRUE THEN
   RAISE EXCEPTION '候选收尾来源、版本、结论或内容摘要不一致'; END IF;
END;
$$ LANGUAGE plpgsql;
"""


_GUARDS = """
CREATE FUNCTION guard_candidate_settlement() RETURNS trigger AS $$
DECLARE run investigation_runs%ROWTYPE; incident incidents%ROWTYPE;
 candidate investigation_candidate_evaluation_jobs%ROWTYPE; wait_version INTEGER;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选收尾事实不可更新或删除'; END IF;
 PERFORM 1 FROM work_items WHERE investigation_run_id=NEW.investigation_run_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION '候选收尾缺少父任务'; END IF;
 SELECT * INTO run FROM investigation_runs WHERE id=NEW.investigation_run_id FOR UPDATE;
 SELECT * INTO candidate FROM investigation_candidate_evaluation_jobs
   WHERE id=NEW.candidate_job_id FOR UPDATE;
 SELECT * INTO incident FROM incidents WHERE id=run.incident_id FOR UPDATE;
 PERFORM 1 FROM candidate_recovery_jobs WHERE id=NEW.recovery_job_id FOR UPDATE;
 PERFORM assert_candidate_settlement_source(NEW);
 SELECT max(state_version) INTO wait_version FROM investigation_human_wait_events
   WHERE investigation_run_id=run.id;
 IF (run.status IN ('await_candidate','await_human') AND run.status=NEW.previous_run_status
   AND run.updated_at=NEW.previous_run_updated_at AND run.completed_at IS NULL
   AND run.result IS NULL AND run.metrics IS NULL AND run.last_error IS NULL
   AND run.workflow_version='v3' AND run.graph_version='dynamic-investigation-graph-v15'
   AND run.data_source_mode='frozen'
   AND incident.status='mitigating' AND incident.version=NEW.incident_version
   AND NEW.verification_version=(SELECT max(version) FROM candidate_condition_verifications
     WHERE candidate_job_id=candidate.id AND investigation_run_id=run.id)
   AND ((candidate.source_kind='terminal_handoff' AND run.status='await_candidate') OR
     (candidate.source_kind='human_route' AND run.status='await_human'
       AND wait_version=candidate.wait_version))
   AND NOT EXISTS (SELECT 1 FROM investigation_human_wait_events
     WHERE investigation_run_id=run.id AND action='cancel')
   AND NOT EXISTS (SELECT 1 FROM investigation_call_reservations
     WHERE investigation_run_id=run.id AND status='reserved')
   AND NOT EXISTS (SELECT 1 FROM investigation_candidate_evaluation_jobs
     WHERE investigation_run_id=run.id AND status IN ('queued','running'))
   AND NOT EXISTS (SELECT 1 FROM candidate_release_execution_jobs
     WHERE investigation_run_id=run.id AND status IN ('queued','running'))
   AND NOT EXISTS (SELECT 1 FROM candidate_recovery_jobs
     WHERE investigation_run_id=run.id AND status<>'completed')
   AND NEW.event_cursor=1+coalesce((SELECT max(cursor) FROM agent_task_events
     WHERE investigation_run_id=run.id),0)
   AND NEW.created_at<=clock_timestamp()+interval '1 second') IS NOT TRUE THEN
   RAISE EXCEPTION '候选收尾需要当前等待、事故版本、最高核验及无在途调用'; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_settlement_guard BEFORE INSERT OR UPDATE OR DELETE
 ON investigation_candidate_settlements FOR EACH ROW EXECUTE FUNCTION guard_candidate_settlement();

CREATE FUNCTION guard_candidate_settlement_run() RETURNS trigger AS $$
DECLARE item investigation_candidate_settlements%ROWTYPE;
BEGIN
 IF TG_OP='UPDATE' AND OLD.status='settled' THEN
   RAISE EXCEPTION '候选已收尾父运行不可改写'; END IF;
 IF NEW.status<>'settled' THEN RETURN NEW; END IF;
 IF TG_OP<>'UPDATE' THEN RAISE EXCEPTION '候选收尾必须由原等待运行迁移'; END IF;
 SELECT * INTO item FROM investigation_candidate_settlements WHERE investigation_run_id=OLD.id;
 IF (OLD.status IN ('await_candidate','await_human') AND item.id IS NOT NULL
   AND item.previous_run_status=OLD.status AND item.previous_run_updated_at=OLD.updated_at
   AND NEW.completed_at=item.created_at AND NEW.updated_at=item.created_at
   AND (to_jsonb(NEW)-ARRAY['status','updated_at','completed_at'])=
       (to_jsonb(OLD)-ARRAY['status','updated_at','completed_at'])) IS NOT TRUE THEN
   RAISE EXCEPTION '候选收尾只允许精确绑定原等待状态和时刻的父运行迁移'; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_settlement_run_guard BEFORE INSERT OR UPDATE ON investigation_runs
 FOR EACH ROW EXECUTE FUNCTION guard_candidate_settlement_run();

CREATE FUNCTION guard_candidate_settlement_event() RETURNS trigger AS $$
DECLARE item investigation_candidate_settlements%ROWTYPE; run investigation_runs%ROWTYPE;
BEGIN
 IF TG_OP<>'INSERT' AND OLD.event_type='run.candidate_settled' THEN
   RAISE EXCEPTION '候选收尾事件不可更新或删除'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 IF NEW.event_type<>'run.candidate_settled' THEN RETURN NEW; END IF;
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选收尾事件不可改写'; END IF;
 PERFORM 1 FROM work_items WHERE investigation_run_id=NEW.investigation_run_id FOR UPDATE;
 SELECT * INTO run FROM investigation_runs WHERE id=NEW.investigation_run_id;
 SELECT * INTO item FROM investigation_candidate_settlements
   WHERE investigation_run_id=NEW.investigation_run_id;
 IF (item.id IS NOT NULL AND item.event_cursor=NEW.cursor AND NEW.task_id IS NULL
   AND item.created_at=NEW.occurred_at AND run.status=item.previous_run_status
   AND run.updated_at=item.previous_run_updated_at
   AND NEW.cursor=1+coalesce((SELECT max(cursor) FROM agent_task_events
     WHERE investigation_run_id=NEW.investigation_run_id),0)) IS NOT TRUE THEN
   RAISE EXCEPTION '候选收尾事件需要同事务事实及连续最新游标'; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_settlement_event_guard BEFORE INSERT OR UPDATE OR DELETE
 ON agent_task_events FOR EACH ROW EXECUTE FUNCTION guard_candidate_settlement_event();
"""


_STATE = """
CREATE FUNCTION validate_candidate_settlement_state() RETURNS trigger AS $$
DECLARE identifier UUID; item investigation_candidate_settlements%ROWTYPE;
 record_tx XID; event_tx XID; run_tx XID;
BEGIN
 IF TG_TABLE_NAME='investigation_runs' THEN
   IF NEW.status<>'settled' THEN RETURN NULL; END IF;
   identifier:=NEW.id;
 ELSIF TG_TABLE_NAME='agent_task_events' THEN
   IF NEW.event_type<>'run.candidate_settled' THEN RETURN NULL; END IF;
   identifier:=NEW.investigation_run_id;
 ELSE identifier:=NEW.investigation_run_id; END IF;
 SELECT * INTO item FROM investigation_candidate_settlements WHERE investigation_run_id=identifier;
 PERFORM assert_candidate_settlement_source(item);
 PERFORM assert_candidate_settlement_leaf(item);
 SELECT xmin INTO record_tx FROM investigation_candidate_settlements WHERE id=item.id;
 SELECT xmin INTO event_tx FROM agent_task_events WHERE investigation_run_id=identifier
   AND cursor=item.event_cursor;
 SELECT xmin INTO run_tx FROM investigation_runs WHERE id=identifier;
 IF record_tx IS DISTINCT FROM event_tx OR record_tx IS DISTINCT FROM run_tx THEN
   RAISE EXCEPTION '候选收尾事实、事件和父终态必须同事务保存'; END IF;
 RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER candidate_settlement_state AFTER INSERT ON
 investigation_candidate_settlements DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION validate_candidate_settlement_state();
CREATE CONSTRAINT TRIGGER candidate_settlement_run_state AFTER INSERT OR UPDATE
 ON investigation_runs
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_candidate_settlement_state();
CREATE CONSTRAINT TRIGGER candidate_settlement_event_state AFTER INSERT ON agent_task_events
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_candidate_settlement_state();
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_candidate_settlements)
          OR EXISTS (SELECT 1 FROM investigation_runs WHERE status='settled')
          OR EXISTS (SELECT 1 FROM agent_task_events WHERE event_type='run.candidate_settled') THEN
          RAISE EXCEPTION '不能删除已保存的候选收尾事实'; END IF;
      END $$;
    """)
    op.execute(_PREVIOUS_HANDOFF_STATE)
    op.execute("""
      DROP FUNCTION validate_candidate_settlement_state() CASCADE;
      DROP FUNCTION guard_candidate_settlement_event() CASCADE;
      DROP FUNCTION guard_candidate_settlement_run() CASCADE;
      DROP FUNCTION guard_candidate_settlement() CASCADE;
      DROP FUNCTION assert_candidate_settlement_source(investigation_candidate_settlements);
      DROP FUNCTION assert_candidate_settlement_leaf(investigation_candidate_settlements);
      DROP FUNCTION candidate_settlement_content(investigation_candidate_settlements);
      DROP TABLE investigation_candidate_settlements;
      ALTER TABLE investigation_runs DROP CONSTRAINT ck_investigation_runs_settled_scope;
    """)
    _lifecycle(settled=False)


_PREVIOUS_HANDOFF_STATE = """
CREATE OR REPLACE FUNCTION validate_terminal_handoff_state() RETURNS trigger AS $$
DECLARE
  run_id UUID;
  run investigation_runs%ROWTYPE;
  handoff investigation_terminal_handoffs%ROWTYPE;
  parent investigation_attempts%ROWTYPE;
  work work_items%ROWTYPE;
  cancellation investigation_human_wait_events%ROWTYPE;
  expected_status TEXT;
BEGIN
  IF TG_TABLE_NAME='investigation_runs' THEN run_id:=NEW.id;
  ELSE run_id:=NEW.investigation_run_id; END IF;
  SELECT * INTO run FROM investigation_runs WHERE id=run_id;
  SELECT * INTO handoff FROM investigation_terminal_handoffs WHERE investigation_run_id=run_id;
  IF handoff.id IS NULL THEN
    IF run.status IN ('await_human','await_candidate','cancelled') OR EXISTS
      (SELECT 1 FROM investigation_attempts
       WHERE investigation_run_id=run_id AND status='handed_off')
    THEN RAISE EXCEPTION '等待状态或交接尝试缺少终端交接事实'; END IF;
    RETURN NULL;
  END IF;
  SELECT * INTO parent FROM investigation_attempts WHERE id=handoff.investigation_attempt_id;
  SELECT * INTO work FROM work_items WHERE investigation_run_id=run_id;
  SELECT * INTO cancellation FROM investigation_human_wait_events
    WHERE investigation_run_id=run_id ORDER BY state_version DESC LIMIT 1;
  expected_status:=CASE handoff.destination WHEN 'await_human' THEN 'await_human'
                       ELSE 'await_candidate' END;
  IF run.status='cancelled' THEN
    IF handoff.destination<>'await_human' OR cancellation.action IS DISTINCT FROM 'cancel'
      OR run.completed_at IS DISTINCT FROM cancellation.created_at THEN
      RAISE EXCEPTION '取消运行缺少最终可信人工取消事件';
    END IF;
  ELSIF run.status IS DISTINCT FROM expected_status OR run.completed_at IS NOT NULL
    OR cancellation.action='cancel' THEN
    RAISE EXCEPTION '终端交接与运行、任务、父尝试状态不一致';
  END IF;
  IF run.result IS NOT NULL OR run.metrics IS NOT NULL OR work.id IS NULL OR parent.id IS NULL
    OR work.status<>'completed' OR work.completed_at IS DISTINCT FROM handoff.created_at
    OR work.locked_by IS NOT NULL OR work.locked_until IS NOT NULL OR work.lease_token IS NOT NULL
    OR parent.status<>'handed_off' OR parent.work_item_id IS DISTINCT FROM work.id
    OR parent.finished_at IS DISTINCT FROM handoff.created_at
    OR EXISTS (SELECT 1 FROM investigation_attempts WHERE investigation_run_id=run_id
               AND status='handed_off' AND id<>handoff.investigation_attempt_id) THEN
    RAISE EXCEPTION '终端交接与运行、任务、父尝试状态不一致';
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


_HANDOFF_STATE = """
CREATE OR REPLACE FUNCTION validate_terminal_handoff_state() RETURNS trigger AS $$
DECLARE
  run_id UUID;
  run investigation_runs%ROWTYPE;
  handoff investigation_terminal_handoffs%ROWTYPE;
  parent investigation_attempts%ROWTYPE;
  work work_items%ROWTYPE;
  cancellation investigation_human_wait_events%ROWTYPE;
  expected_status TEXT;
BEGIN
  IF TG_TABLE_NAME='investigation_runs' THEN run_id:=NEW.id;
  ELSE run_id:=NEW.investigation_run_id; END IF;
  SELECT * INTO run FROM investigation_runs WHERE id=run_id;
  SELECT * INTO handoff FROM investigation_terminal_handoffs WHERE investigation_run_id=run_id;
  IF handoff.id IS NULL THEN
    IF run.status IN ('await_human','await_candidate','cancelled','settled') OR EXISTS
      (SELECT 1 FROM investigation_attempts
       WHERE investigation_run_id=run_id AND status='handed_off')
    THEN RAISE EXCEPTION '等待状态或交接尝试缺少终端交接事实'; END IF;
    RETURN NULL;
  END IF;
  SELECT * INTO parent FROM investigation_attempts WHERE id=handoff.investigation_attempt_id;
  SELECT * INTO work FROM work_items WHERE investigation_run_id=run_id;
  SELECT * INTO cancellation FROM investigation_human_wait_events
    WHERE investigation_run_id=run_id ORDER BY state_version DESC LIMIT 1;
  expected_status:=CASE handoff.destination WHEN 'await_human' THEN 'await_human'
                       ELSE 'await_candidate' END;
  IF run.status='settled' THEN
    PERFORM assert_candidate_settlement_leaf((SELECT item
      FROM investigation_candidate_settlements item WHERE investigation_run_id=run_id));
    IF cancellation.action='cancel' THEN RAISE EXCEPTION '已取消候选不能收尾'; END IF;
  ELSIF run.status='cancelled' THEN
    IF handoff.destination<>'await_human' OR cancellation.action IS DISTINCT FROM 'cancel'
      OR run.completed_at IS DISTINCT FROM cancellation.created_at THEN
      RAISE EXCEPTION '取消运行缺少最终可信人工取消事件';
    END IF;
  ELSIF run.status IS DISTINCT FROM expected_status OR run.completed_at IS NOT NULL
    OR cancellation.action='cancel' THEN
    RAISE EXCEPTION '终端交接与运行、任务、父尝试状态不一致';
  END IF;
  IF run.result IS NOT NULL OR run.metrics IS NOT NULL OR work.id IS NULL OR parent.id IS NULL
    OR work.status<>'completed' OR work.completed_at IS DISTINCT FROM handoff.created_at
    OR work.locked_by IS NOT NULL OR work.locked_until IS NOT NULL OR work.lease_token IS NOT NULL
    OR parent.status<>'handed_off' OR parent.work_item_id IS DISTINCT FROM work.id
    OR parent.finished_at IS DISTINCT FROM handoff.created_at
    OR EXISTS (SELECT 1 FROM investigation_attempts WHERE investigation_run_id=run_id
               AND status='handed_off' AND id<>handoff.investigation_attempt_id) THEN
    RAISE EXCEPTION '终端交接与运行、任务、父尝试状态不一致';
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""
