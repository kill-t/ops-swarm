"""候选收尾与 Incident 两跳、事件及槽位投影的受控收敛。"""

from alembic import op

revision = "0085_candidate_convergence"
down_revision = "0084_candidate_settlement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_TABLE)
    op.execute(_EXPECTED)
    op.execute(_HISTORY)
    op.execute(_GUARDS)
    op.execute(_STATE)
    op.execute(_SETTLEMENT)


_TABLE = """
CREATE TABLE candidate_incident_convergences (
 investigation_run_id UUID PRIMARY KEY REFERENCES
   investigation_candidate_settlements(investigation_run_id) ON DELETE RESTRICT,
 incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE RESTRICT,
 content JSONB NOT NULL,
 content_sha256 VARCHAR(64) NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 CONSTRAINT ck_candidate_convergence_content CHECK
   (jsonb_typeof(content)='object' AND octet_length(content::text)<=16384
    AND content_sha256 ~ '^[0-9a-f]{64}$')
);
ALTER TABLE investigation_runs ADD COLUMN candidate_slot_released_at TIMESTAMPTZ;
ALTER TABLE investigation_runs ADD CONSTRAINT ck_investigation_runs_candidate_slot CHECK
 (candidate_slot_released_at IS NULL OR
   (status='settled' AND isfinite(candidate_slot_released_at)));
DROP INDEX uq_investigation_runs_active_incident;
CREATE UNIQUE INDEX uq_investigation_runs_active_incident ON investigation_runs(incident_id)
 WHERE status IN ('queued','running','await_human','await_candidate')
 OR (status='settled' AND candidate_slot_released_at IS NULL);
"""


_EXPECTED = """
-- 历史只核验保存的告警引用；等待事故锁的合法告警可能在决定后才入库。
CREATE FUNCTION candidate_convergence_expected(item candidate_incident_convergences)
 RETURNS JSONB AS $$
DECLARE settlement investigation_candidate_settlements%ROWTYPE;
 run investigation_runs%ROWTYPE; job candidate_recovery_jobs%ROWTYPE;
 result candidate_recovery_results%ROWTYPE; alert alerts%ROWTYPE;
 body JSONB; before_status TEXT; before_version INTEGER; after_status TEXT;
 after_version INTEGER; outcome TEXT; reason TEXT; actions JSONB:='[]'::jsonb;
 fresh_until TIMESTAMPTZ; first_observed TIMESTAMPTZ; last_observed TIMESTAMPTZ;
 count_samples INTEGER; released BOOLEAN:=FALSE;
BEGIN
 SELECT * INTO settlement FROM investigation_candidate_settlements
   WHERE investigation_run_id=item.investigation_run_id;
 IF settlement.id IS NULL THEN RAISE EXCEPTION '事故收敛缺少候选收尾来源'; END IF;
 PERFORM assert_candidate_settlement_source(settlement);
 PERFORM assert_candidate_settlement_leaf(settlement);
 SELECT * INTO run FROM investigation_runs WHERE id=settlement.investigation_run_id;
 SELECT * INTO job FROM candidate_recovery_jobs WHERE id=settlement.recovery_job_id;
 SELECT * INTO result FROM candidate_recovery_results WHERE job_id=job.id;
 body:=item.content;
 before_status:=body->>'before_status';
 IF (jsonb_typeof(body->'before_version')='number'
   AND body->>'before_version' ~ '^[1-9][0-9]{0,8}$'
   AND before_status IN ('open','investigating','awaiting_approval','mitigating','verifying',
     'resolved','closed')) IS NOT TRUE THEN
   RAISE EXCEPTION '事故收敛前置状态或版本不合法'; END IF;
 before_version:=(body->>'before_version')::integer;
 IF NOT EXISTS (SELECT 1 FROM incident_events WHERE incident_id=item.incident_id
   AND incident_version=before_version AND to_status=before_status
   AND event_type IN ('incident.created','incident.status_changed')
   AND occurred_at<=item.created_at) THEN
   RAISE EXCEPTION '事故收敛前置状态和版本缺少既有事故事件'; END IF;
 after_status:=before_status; after_version:=before_version;
 SELECT * INTO alert FROM alerts WHERE id=(body->>'latest_alert_id')::uuid
   AND incident_id=item.incident_id AND ingested_at<=item.created_at;
 IF result.content->>'outcome'='recovered' OR
   (result.content->>'outcome'='not_recovered'
     AND result.content->>'reason_code'='business_probe_failed') THEN
   SELECT count(*),min(candidate_release_timestamp(observation->'observed_at')),
     max(candidate_release_timestamp(observation->'observed_at'))
     INTO count_samples,first_observed,last_observed FROM candidate_recovery_samples
     WHERE attempt_id=(result.content->>'attempt_id')::uuid AND job_id=job.id
       AND observation IS NOT NULL AND error_code IS NULL;
   IF count_samples<>3 OR first_observed IS NULL OR last_observed IS NULL THEN
     RAISE EXCEPTION '事故收敛需要最终同一尝试的三份业务样本'; END IF;
   fresh_until:=least(job.window_end,first_observed+interval '120 seconds');
 END IF;
 IF before_status<>'mitigating' OR before_version<>settlement.incident_version THEN
   outcome:='held_incident_changed'; reason:='incident_basis_changed';
 ELSIF fresh_until IS NULL THEN
   outcome:='held_uncertain'; reason:=result.content->>'reason_code';
 ELSIF item.created_at>=fresh_until THEN
   outcome:='held_stale'; reason:='recovery_samples_stale';
 ELSIF alert.ingested_at>last_observed THEN
   outcome:='held_new_alert'; reason:='new_alert_after_samples';
 ELSE
   after_version:=before_version+2;
   IF result.content->>'outcome'='recovered' THEN
     outcome:='recovered'; reason:='fresh_recovery_verified'; after_status:='resolved';
     actions:='["complete_mitigation","confirm_recovery"]'::jsonb; released:=TRUE;
   ELSE
     outcome:='observed_failure'; reason:='fresh_verification_failed';
     after_status:='investigating';
     actions:='["complete_mitigation","fail_verification"]'::jsonb;
   END IF;
 END IF;
 IF (item.incident_id=run.incident_id AND isfinite(item.created_at)
   AND item.created_at>=settlement.created_at) IS NOT TRUE THEN
   RAISE EXCEPTION '事故收敛缺少精确父运行、事故或创建时刻'; END IF;
 RETURN jsonb_build_object(
   'policy_version','candidate-incident-convergence-v1',
   'investigation_run_id',item.investigation_run_id::text,'incident_id',item.incident_id::text,
   'settlement_content_sha256',settlement.content_sha256,
   'before_status',before_status,'before_version',before_version,
   'after_status',after_status,'after_version',after_version,'outcome',outcome,'reason_code',reason,
   'fresh_until',candidate_recovery_utc_text(fresh_until),'latest_alert_id',alert.id::text,
   'latest_alert_ingested_at',candidate_recovery_utc_text(alert.ingested_at),
   'actions',actions,'slot_released',released,
   'created_at',candidate_recovery_utc_text(item.created_at));
END;
$$ LANGUAGE plpgsql;
"""


_HISTORY = """
CREATE FUNCTION assert_candidate_convergence_history(item candidate_incident_convergences)
 RETURNS VOID AS $$
DECLARE body JSONB; event incident_events%ROWTYPE; step INTEGER;
 previous_status TEXT; next_status TEXT; run investigation_runs%ROWTYPE;
BEGIN
 body:=candidate_convergence_expected(item);
 IF (item.content=body AND item.content_sha256=encode(sha256(convert_to(
   candidate_canonical_json(body),'UTF8')),'hex')) IS NOT TRUE THEN
   RAISE EXCEPTION '事故收敛正文、确定性结论或内容摘要不一致'; END IF;
 SELECT * INTO run FROM investigation_runs WHERE id=item.investigation_run_id;
 IF (run.candidate_slot_released_at IS NOT DISTINCT FROM
   CASE WHEN body->'slot_released'='true'::jsonb THEN item.created_at ELSE NULL END)
   IS NOT TRUE THEN RAISE EXCEPTION '事故收敛槽位投影不一致'; END IF;
 IF (SELECT count(*) FROM incident_events WHERE
   actor_id='candidate-convergence:'||item.investigation_run_id::text)<>
     jsonb_array_length(body->'actions') THEN
   RAISE EXCEPTION '事故收敛事件数量与动作不一致'; END IF;
 previous_status:=body->>'before_status';
 FOR step IN 1..jsonb_array_length(body->'actions') LOOP
   next_status:=CASE WHEN step=1 THEN 'verifying' ELSE body->>'after_status' END;
   SELECT * INTO event FROM incident_events WHERE incident_id=item.incident_id
     AND incident_version=(body->>'before_version')::integer+step
     AND event_type='incident.status_changed';
   IF (event.actor_type='system'
     AND event.actor_id='candidate-convergence:'||item.investigation_run_id::text
     AND event.from_status=previous_status AND event.to_status=next_status
     AND event.reason=body->>'reason_code' AND event.occurred_at=item.created_at
     AND event.payload=jsonb_build_object('action',body->'actions'->(step-1))) IS NOT TRUE THEN
     RAISE EXCEPTION '事故收敛缺少精确绑定动作、状态、原因和时刻的事件'; END IF;
   previous_status:=next_status;
 END LOOP;
END;
$$ LANGUAGE plpgsql;
"""


_GUARDS = """
CREATE FUNCTION guard_candidate_convergence() RETURNS trigger AS $$
DECLARE settlement investigation_candidate_settlements%ROWTYPE;
 incident incidents%ROWTYPE; alert alerts%ROWTYPE; expected JSONB;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '事故收敛事实不可更新或删除'; END IF;
 PERFORM 1 FROM work_items WHERE investigation_run_id=NEW.investigation_run_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION '事故收敛缺少父任务'; END IF;
 PERFORM 1 FROM investigation_runs WHERE id=NEW.investigation_run_id FOR UPDATE;
 SELECT * INTO settlement FROM investigation_candidate_settlements
   WHERE investigation_run_id=NEW.investigation_run_id;
 PERFORM 1 FROM investigation_candidate_evaluation_jobs WHERE id=settlement.candidate_job_id
   FOR UPDATE;
 SELECT * INTO incident FROM incidents WHERE id=NEW.incident_id FOR UPDATE;
 expected:=candidate_convergence_expected(NEW);
 SELECT * INTO alert FROM alerts WHERE incident_id=NEW.incident_id
   ORDER BY ingested_at DESC,id DESC LIMIT 1;
 IF (NEW.content=expected
   AND NEW.content_sha256=encode(sha256(convert_to(
     candidate_canonical_json(expected),'UTF8')),'hex')
   AND NEW.content->>'before_status'=incident.status
   AND NEW.content->'before_version'=to_jsonb(incident.version)
   AND NEW.created_at>=incident.updated_at AND NEW.created_at<=clock_timestamp()+interval '1 second'
   AND (NEW.content->>'latest_alert_id') IS NOT DISTINCT FROM alert.id::text
   AND (NEW.content->>'latest_alert_ingested_at') IS NOT DISTINCT FROM
     candidate_recovery_utc_text(alert.ingested_at)
   AND (alert.id IS NULL OR alert.ingested_at<=NEW.created_at)) IS NOT TRUE THEN
   RAISE EXCEPTION '事故收敛需要当前事故版本、最新告警、确定性来源和实际时钟'; END IF;
 IF NEW.content->>'outcome' IN ('recovered','observed_failure') AND
   clock_timestamp()>=candidate_release_timestamp(NEW.content->'fresh_until') THEN
   RAISE EXCEPTION '事故收敛业务样本已过期'; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_convergence_guard BEFORE INSERT OR UPDATE OR DELETE
 ON candidate_incident_convergences FOR EACH ROW EXECUTE FUNCTION guard_candidate_convergence();

CREATE FUNCTION guard_candidate_convergence_event() RETURNS trigger AS $$
DECLARE item candidate_incident_convergences%ROWTYPE; step INTEGER; body JSONB;
BEGIN
 IF TG_OP<>'INSERT' AND OLD.actor_id LIKE 'candidate-convergence:%' THEN
   RAISE EXCEPTION '事故收敛事件不可更新或删除'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 IF NEW.actor_id NOT LIKE 'candidate-convergence:%' OR NEW.actor_id IS NULL THEN RETURN NEW; END IF;
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '事故收敛事件不可改写'; END IF;
 SELECT * INTO item FROM candidate_incident_convergences
   WHERE 'candidate-convergence:'||investigation_run_id::text=NEW.actor_id;
 body:=item.content; step:=NEW.incident_version-(body->>'before_version')::integer;
 IF (item.investigation_run_id IS NOT NULL AND item.incident_id=NEW.incident_id
   AND (SELECT xmin FROM candidate_incident_convergences
     WHERE investigation_run_id=item.investigation_run_id)::text=pg_current_xact_id()::text
   AND step IN (1,2) AND jsonb_array_length(body->'actions')=2
   AND NEW.event_type='incident.status_changed' AND NEW.actor_type='system'
   AND NEW.from_status=CASE step WHEN 1 THEN body->>'before_status' ELSE 'verifying' END
   AND NEW.to_status=CASE step WHEN 1 THEN 'verifying' ELSE body->>'after_status' END
   AND NEW.reason=body->>'reason_code' AND NEW.occurred_at=item.created_at
   AND NEW.payload=jsonb_build_object('action',body->'actions'->(step-1))) IS NOT TRUE THEN
   RAISE EXCEPTION '事故收敛事件需要同事务精确事实'; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_convergence_event_guard BEFORE INSERT OR UPDATE OR DELETE
 ON incident_events FOR EACH ROW EXECUTE FUNCTION guard_candidate_convergence_event();

-- 只封住尚未形成130决定的settled来源；已有决定后的close/reopen仍走既有生命周期。
CREATE FUNCTION guard_candidate_convergence_incident() RETURNS trigger AS $$
DECLARE item candidate_incident_convergences%ROWTYPE; step INTEGER;
BEGIN
 IF EXISTS (SELECT 1 FROM investigation_runs run
   JOIN investigation_candidate_settlements settlement ON settlement.investigation_run_id=run.id
   WHERE run.incident_id=OLD.id AND run.status='settled'
     AND (NOT EXISTS (SELECT 1 FROM candidate_incident_convergences saved
       WHERE saved.investigation_run_id=run.id)
       OR (OLD.status='mitigating' AND OLD.version=settlement.incident_version
         AND EXISTS (SELECT 1 FROM candidate_incident_convergences saved
           WHERE saved.investigation_run_id=run.id AND saved.content->>'outcome'
             IN ('held_uncertain','held_stale','held_new_alert')))))
   AND (NEW.status IS DISTINCT FROM OLD.status OR NEW.version IS DISTINCT FROM OLD.version
     OR NEW.resolved_at IS DISTINCT FROM OLD.resolved_at
     OR NEW.closed_at IS DISTINCT FROM OLD.closed_at) THEN
   RAISE EXCEPTION '候选已收尾事故迁移必须先保存受控收敛事实'; END IF;
 SELECT * INTO item FROM candidate_incident_convergences WHERE incident_id=OLD.id
   AND xmin::text=pg_current_xact_id()::text;
 IF item.investigation_run_id IS NOT NULL THEN
   step:=NEW.version-(item.content->>'before_version')::integer;
   IF (jsonb_array_length(item.content->'actions')=2 AND step IN (1,2)
     AND NEW.version=OLD.version+1
     AND OLD.status=CASE step WHEN 1 THEN item.content->>'before_status' ELSE 'verifying' END
     AND NEW.status=CASE step WHEN 1 THEN 'verifying' ELSE item.content->>'after_status' END
     AND NEW.updated_at=item.created_at AND NEW.closed_at IS NULL
     AND NEW.resolved_at IS NOT DISTINCT FROM CASE
       WHEN step=2 AND item.content->>'outcome'='recovered' THEN item.created_at ELSE NULL END
     AND (to_jsonb(NEW)-ARRAY['status','version','updated_at','resolved_at','closed_at'])=
       (to_jsonb(OLD)-ARRAY['status','version','updated_at','resolved_at','closed_at']))
     IS NOT TRUE THEN RAISE EXCEPTION '事故收敛必须逐个执行相邻版本的受控两跳'; END IF;
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_convergence_incident_guard BEFORE UPDATE ON incidents
 FOR EACH ROW EXECUTE FUNCTION guard_candidate_convergence_incident();

-- 原生告警写入也持有事故锁；晚于决定提交的请求不能改变已保存的引用。
CREATE FUNCTION guard_candidate_convergence_alert() RETURNS trigger AS $$
DECLARE identifier UUID;
BEGIN
 identifier:=CASE WHEN TG_OP='DELETE' THEN OLD.incident_id ELSE NEW.incident_id END;
 PERFORM 1 FROM incidents WHERE id=identifier FOR UPDATE;
 IF TG_OP<>'INSERT' AND EXISTS (SELECT 1 FROM candidate_incident_convergences item
   WHERE item.content->>'latest_alert_id'=OLD.id::text) THEN
   RAISE EXCEPTION '事故收敛引用的告警不可改写或删除'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_convergence_alert_guard BEFORE INSERT OR UPDATE OR DELETE
 ON alerts FOR EACH ROW EXECUTE FUNCTION guard_candidate_convergence_alert();
"""


_STATE = """
CREATE FUNCTION validate_candidate_convergence_state() RETURNS trigger AS $$
DECLARE identifier UUID; item candidate_incident_convergences%ROWTYPE;
 incident incidents%ROWTYPE; record_tx XID; incident_tx XID; run_tx XID; event_tx XID;
 event incident_events%ROWTYPE;
BEGIN
 IF TG_TABLE_NAME='investigation_runs' THEN
   IF NEW.candidate_slot_released_at IS NULL THEN RETURN NULL; END IF;
   identifier:=NEW.id;
 ELSIF TG_TABLE_NAME='incident_events' THEN
   IF NEW.actor_id NOT LIKE 'candidate-convergence:%' OR NEW.actor_id IS NULL THEN
     RETURN NULL; END IF;
   SELECT investigation_run_id INTO identifier FROM candidate_incident_convergences
     WHERE 'candidate-convergence:'||investigation_run_id::text=NEW.actor_id;
 ELSE identifier:=NEW.investigation_run_id; END IF;
 SELECT * INTO item FROM candidate_incident_convergences WHERE investigation_run_id=identifier;
 IF item.investigation_run_id IS NULL THEN RAISE EXCEPTION '事故收敛缺少同事务事实'; END IF;
 PERFORM assert_candidate_convergence_history(item);
 SELECT xmin INTO record_tx FROM candidate_incident_convergences
   WHERE investigation_run_id=identifier;
 SELECT * INTO incident FROM incidents WHERE id=item.incident_id;
 SELECT xmin INTO incident_tx FROM incidents WHERE id=item.incident_id;
 SELECT xmin INTO run_tx FROM investigation_runs WHERE id=identifier;
 IF (incident.status=item.content->>'after_status'
   AND incident.version=(item.content->>'after_version')::integer) IS NOT TRUE THEN
   RAISE EXCEPTION '事故收敛缺少同事务事故终态'; END IF;
 IF jsonb_array_length(item.content->'actions')=0 THEN
   IF incident_tx=record_tx THEN RAISE EXCEPTION '保留决定不能改写事故'; END IF;
 ELSE
   IF (incident_tx IS DISTINCT FROM record_tx OR incident.updated_at<>item.created_at
     OR incident.closed_at IS NOT NULL OR incident.resolved_at IS DISTINCT FROM
       CASE WHEN item.content->>'outcome'='recovered' THEN item.created_at ELSE NULL END) THEN
     RAISE EXCEPTION '事故收敛事实与事故终态必须同事务保存'; END IF;
   IF clock_timestamp()>=candidate_release_timestamp(item.content->'fresh_until') THEN
     RAISE EXCEPTION '事故收敛提交时业务样本已过期'; END IF;
 END IF;
 FOR event IN SELECT * FROM incident_events
   WHERE actor_id='candidate-convergence:'||identifier::text LOOP
   SELECT xmin INTO event_tx FROM incident_events WHERE id=event.id;
   IF event_tx IS DISTINCT FROM record_tx THEN
     RAISE EXCEPTION '事故收敛事实和全部事件必须同事务保存'; END IF;
 END LOOP;
 IF item.content->'slot_released'='true'::jsonb AND run_tx IS DISTINCT FROM record_tx THEN
   RAISE EXCEPTION '事故收敛槽位必须与成功事实同事务保存'; END IF;
 RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER candidate_convergence_state AFTER INSERT
 ON candidate_incident_convergences DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION validate_candidate_convergence_state();
CREATE CONSTRAINT TRIGGER candidate_convergence_event_state AFTER INSERT
 ON incident_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION validate_candidate_convergence_state();
CREATE CONSTRAINT TRIGGER candidate_convergence_run_state AFTER INSERT OR UPDATE
 ON investigation_runs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION validate_candidate_convergence_state();
"""


_SETTLEMENT = """
CREATE OR REPLACE FUNCTION guard_candidate_settlement_run() RETURNS trigger AS $$
DECLARE item investigation_candidate_settlements%ROWTYPE;
 convergence candidate_incident_convergences%ROWTYPE;
BEGIN
 IF TG_OP='UPDATE' AND OLD.status='settled' THEN
   SELECT * INTO convergence FROM candidate_incident_convergences
     WHERE investigation_run_id=OLD.id;
   IF (OLD.candidate_slot_released_at IS NULL
     AND NEW.candidate_slot_released_at=convergence.created_at
     AND convergence.content->'slot_released'='true'::jsonb
     AND convergence.content->>'outcome'='recovered'
     AND (SELECT xmin FROM candidate_incident_convergences
       WHERE investigation_run_id=OLD.id)::text=pg_current_xact_id()::text
     AND (to_jsonb(NEW)-'candidate_slot_released_at')=
       (to_jsonb(OLD)-'candidate_slot_released_at')) IS NOT TRUE THEN
     RAISE EXCEPTION '候选已收尾父运行不可改写，仅允许同事务成功收敛释放槽位'; END IF;
   RETURN NEW;
 END IF;
 IF NEW.candidate_slot_released_at IS NOT NULL THEN
   RAISE EXCEPTION '候选槽位只能由已收尾运行的成功收敛释放'; END IF;
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
CREATE OR REPLACE FUNCTION validate_candidate_settlement_state() RETURNS trigger AS $$
DECLARE identifier UUID; item investigation_candidate_settlements%ROWTYPE;
 record_tx XID; event_tx XID; run_tx XID;
BEGIN
 IF TG_TABLE_NAME='investigation_runs' THEN
   IF NEW.status<>'settled' THEN RETURN NULL; END IF;
   IF TG_OP='UPDATE' AND OLD.status='settled' THEN RETURN NULL; END IF;
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
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM candidate_incident_convergences) OR EXISTS
          (SELECT 1 FROM investigation_runs WHERE candidate_slot_released_at IS NOT NULL) THEN
          RAISE EXCEPTION '不能删除已保存的事故收敛事实'; END IF;
      END $$;
      DROP FUNCTION validate_candidate_convergence_state() CASCADE;
      DROP FUNCTION guard_candidate_convergence_alert() CASCADE;
      DROP FUNCTION guard_candidate_convergence_incident() CASCADE;
      DROP FUNCTION guard_candidate_convergence_event() CASCADE;
      DROP FUNCTION guard_candidate_convergence() CASCADE;
      DROP FUNCTION assert_candidate_convergence_history(candidate_incident_convergences);
      DROP FUNCTION candidate_convergence_expected(candidate_incident_convergences);
      DROP TABLE candidate_incident_convergences;
      DROP INDEX uq_investigation_runs_active_incident;
      CREATE UNIQUE INDEX uq_investigation_runs_active_incident ON investigation_runs(incident_id)
        WHERE status IN ('queued','running','await_human','await_candidate','settled');
      ALTER TABLE investigation_runs DROP CONSTRAINT ck_investigation_runs_candidate_slot;
      ALTER TABLE investigation_runs DROP COLUMN candidate_slot_released_at;
    """)
    op.execute(_PREVIOUS_SETTLEMENT)


_PREVIOUS_SETTLEMENT = """
CREATE OR REPLACE FUNCTION guard_candidate_settlement_run() RETURNS trigger AS $$
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
CREATE OR REPLACE FUNCTION validate_candidate_settlement_state() RETURNS trigger AS $$
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
"""
