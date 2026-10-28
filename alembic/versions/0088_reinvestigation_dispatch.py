"""完整回流建议的确定性编译、原容量与整轮同事务原生约束。"""

from alembic import op

revision = "0088_reflow_dispatch"
down_revision = "0087_reflow_coordination"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(_COMPILER)
    op.execute(_GUARDS)


_TABLES = """
CREATE TABLE investigation_reflow_dispatches (
 investigation_run_id UUID PRIMARY KEY REFERENCES investigation_reflow_results(job_id)
   ON DELETE RESTRICT,
 source_result_sha256 VARCHAR(64) NOT NULL,
 status VARCHAR(16) NOT NULL,
 content JSONB NOT NULL,
 content_sha256 VARCHAR(64) NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 CONSTRAINT ck_reflow_dispatches_status CHECK (status IN ('queued','blocked')),
 CONSTRAINT ck_reflow_dispatches_content CHECK (jsonb_typeof(content)='object'
   AND octet_length(content::text)<=1048576 AND content_sha256 ~ '^[0-9a-f]{64}$'
   AND source_result_sha256 ~ '^[0-9a-f]{64}$')
);
CREATE TABLE investigation_reflow_task_queue (
 id UUID PRIMARY KEY,
 investigation_run_id UUID NOT NULL REFERENCES
   investigation_reflow_dispatches(investigation_run_id) ON DELETE RESTRICT,
 round_id UUID NOT NULL,
 task_key VARCHAR(64) NOT NULL,
 assignment JSONB NOT NULL,
 assignment_sha256 VARCHAR(64) NOT NULL,
 status VARCHAR(16) NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 CONSTRAINT uq_reflow_queue_run_key UNIQUE(investigation_run_id,task_key),
 CONSTRAINT uq_reflow_queue_id_run UNIQUE(id,investigation_run_id),
 CONSTRAINT ck_reflow_queue_status CHECK (status='pending'),
 CONSTRAINT ck_reflow_queue_assignment CHECK (jsonb_typeof(assignment)='object'
   AND octet_length(assignment::text)<=8192 AND assignment_sha256 ~ '^[0-9a-f]{64}$')
);
"""


_COMPILER = """
CREATE FUNCTION reflow_dispatch_id(run_id UUID,name TEXT) RETURNS UUID AS $$
 SELECT substr(encode(sha256(convert_to(run_id::text||':'||name,'UTF8')),'hex'),1,32)::uuid;
$$ LANGUAGE SQL IMMUTABLE STRICT;

CREATE FUNCTION assert_reflow_dispatch_source(run_id UUID) RETURNS VOID AS $$
DECLARE job investigation_reflow_jobs%ROWTYPE; result investigation_reflow_results%ROWTYPE;
 attempt investigation_reflow_attempts%ROWTYPE; call investigation_call_reservations%ROWTYPE;
 caller investigation_reflow_attempts%ROWTYPE;
 incident incidents%ROWTYPE; mutation incident_events%ROWTYPE;
 expected JSONB;
BEGIN
 SELECT * INTO job FROM investigation_reflow_jobs WHERE id=run_id;
 SELECT * INTO result FROM investigation_reflow_results WHERE job_id=run_id;
 SELECT * INTO attempt FROM investigation_reflow_attempts WHERE id=result.attempt_id;
 SELECT * INTO call FROM investigation_call_reservations WHERE id=result.source_call_id;
 SELECT * INTO caller FROM investigation_reflow_attempts WHERE id=call.reinvestigation_attempt_id;
 SELECT i.* INTO incident FROM incidents i JOIN investigation_runs r ON r.incident_id=i.id
   WHERE r.id=run_id;
 SELECT * INTO mutation FROM incident_events WHERE incident_id=incident.id
   AND event_type IN ('incident.created','incident.status_changed') ORDER BY id DESC LIMIT 1;
 IF (incident.version=mutation.incident_version AND incident.status=mutation.to_status) IS NOT TRUE
 THEN RAISE EXCEPTION '回流分派当前事故与持久状态事件不一致'; END IF;
 PERFORM assert_reflow_source(job,FALSE);
 IF (job.status='succeeded' AND job.investigation_run_id=run_id
   AND result.investigation_run_id=run_id AND attempt.job_id=run_id
   AND attempt.investigation_run_id=run_id AND attempt.status='succeeded'
   AND attempt.number=job.attempt_count AND attempt.finished_at=result.created_at
   AND (SELECT count(*) FROM investigation_reflow_attempts WHERE job_id=run_id)=job.attempt_count
   AND NOT EXISTS(SELECT 1 FROM investigation_reflow_attempts WHERE job_id=run_id
     AND number<attempt.number AND status IN ('running','succeeded'))
   AND job.completed_at=result.created_at AND result.created_at>=attempt.started_at
   AND result.created_at<attempt.lease_expires_at
   AND call.investigation_run_id=run_id AND caller.job_id=run_id
   AND caller.investigation_run_id=run_id AND caller.number<=attempt.number
   AND caller.status IN ('succeeded','failed','lease_expired')
   AND call.investigation_attempt_id=(job.source->>'parent_attempt_id')::uuid
   AND call.human_review_attempt_id IS NULL AND call.candidate_attempt_id IS NULL
   AND call.task_id IS NULL AND call.task_attempt_id IS NULL
   AND call.operation='coordinate_reinvestigation' AND call.kind='model'
   AND call.status='succeeded' AND call.error_code IS NULL
   AND call.reserved_at>=caller.started_at AND call.reserved_at<caller.lease_expires_at
   AND call.finished_at>=call.reserved_at AND call.finished_at<caller.lease_expires_at
   AND caller.finished_at>=call.finished_at AND caller.finished_at<=result.created_at
   AND call.finished_at<=result.created_at AND call.invocation_index=0
   AND call.idempotency_key='reflow:'||caller.id::text
   AND call.model_input=job.packet->'model_input'
   AND call.model_allocation=job.packet->'allocation'
   AND call.context_manifest=job.packet->'manifest'
   AND call.call_result->>'kind'='model'
   AND call.call_result->>'version'='reinvestigation-coordination-result-v1'
   AND call.call_result=jsonb_build_object('kind','model',
     'version','reinvestigation-coordination-result-v1',
     'proposal',call.call_result->'proposal')
   AND (SELECT count(*) FROM investigation_call_reservations c
     JOIN investigation_reflow_attempts a ON a.id=c.reinvestigation_attempt_id
     WHERE a.job_id=run_id AND c.status='succeeded')=1) IS NOT TRUE
 THEN RAISE EXCEPTION '回流分派缺少完整原运行成功阶段、尝试、调用与冻结输入闭环'; END IF;
 expected:=jsonb_build_object('investigation_run_id',run_id::text,'job_id',run_id::text,
   'source_call_id',call.id::text,'source_sha256',job.source_sha256,
   'proposal',call.call_result->'proposal','dispatch_status','not_dispatched',
   'created_at',candidate_recovery_utc_text(result.created_at));
 IF (result.content=expected AND result.content_sha256=reflow_hash(expected)) IS NOT TRUE
 THEN RAISE EXCEPTION '回流分派来源建议与真实成功调用不一致'; END IF;
 PERFORM assert_reflow_proposal(result.content->'proposal',job.source);
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION reflow_dispatch_capacity(budget JSONB,total INTEGER,created TIMESTAMPTZ)
 RETURNS TEXT AS $$
DECLARE policy JSONB:=budget->'policy'; model JSONB:=budget->'model_budget';
 usage JSONB:=budget->'model_usage'; cost NUMERIC;
BEGIN
 IF created>=(budget->>'deadline')::timestamptz THEN RETURN 'run_deadline_exceeded'; END IF;
 IF EXISTS(SELECT 1 FROM jsonb_array_elements(budget->'calls') c WHERE c->>'status'='reserved')
 THEN RETURN 'calls_in_flight'; END IF;
 IF (budget->>'reserved_model_calls')::integer+total>(policy->>'max_model_calls')::integer
 THEN RETURN 'model_call_budget_exceeded'; END IF;
 IF (budget->>'reserved_tool_calls')::integer+total>(policy->>'max_tool_calls')::integer
 OR EXISTS(SELECT 1 FROM jsonb_array_elements(budget->'calls') c WHERE c->>'kind'='tool'
   GROUP BY c->>'task_id' HAVING count(*)>(policy->>'max_tool_calls_per_task')::integer)
 THEN RETURN 'tool_call_budget_exceeded'; END IF;
 IF (usage->>'committed_input_tokens')::bigint+total*(model->>'max_input_tokens_per_call')::bigint
   >(model->>'max_input_tokens')::bigint THEN RETURN 'run_input_budget_exceeded'; END IF;
 IF (usage->>'committed_output_tokens')::bigint+total*(model->>'max_output_tokens_per_call')::bigint
   >(model->>'max_output_tokens')::bigint THEN RETURN 'run_output_budget_exceeded'; END IF;
 cost:=reinvestigation_model_cost(model,(model->>'max_input_tokens_per_call')::bigint,
   (model->>'max_output_tokens_per_call')::bigint);
 IF (usage->>'committed_cost_usd')::numeric+total*cost>(model->>'max_estimated_cost_usd')::numeric
 THEN RETURN 'model_cost_budget_exceeded'; END IF;
 IF EXISTS(SELECT 1 FROM jsonb_array_elements(budget->'calls') c
   WHERE c->>'kind'='model' AND c->'model_usage'<>'null'::jsonb AND
   ((c#>>'{model_usage,input_tokens}')::bigint>(c#>>'{model_allocation,estimated_input_tokens}')::bigint
    OR (c#>>'{model_usage,output_tokens}')::bigint>
      (c#>>'{model_allocation,output_token_limit}')::bigint
    OR reinvestigation_model_cost(model,(c#>>'{model_usage,input_tokens}')::bigint,
      (c#>>'{model_usage,output_tokens}')::bigint)>(c#>>'{model_allocation,reserved_cost_usd}')::numeric))
 THEN RETURN 'model_usage_exceeded_allocation'; END IF;
 RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION reflow_dispatch_content(run_id UUID,created TIMESTAMPTZ) RETURNS JSONB AS $$
DECLARE job investigation_reflow_jobs%ROWTYPE; result investigation_reflow_results%ROWTYPE;
 source JSONB; proposal JSONB; budget JSONB; floor_at TIMESTAMPTZ; service TEXT; reason TEXT;
 assignment JSONB:=NULL; tasks JSONB:='[]'; task JSONB; round_id UUID:=NULL;
BEGIN
 PERFORM assert_reflow_dispatch_source(run_id);
 SELECT * INTO job FROM investigation_reflow_jobs WHERE id=run_id;
 SELECT * INTO result FROM investigation_reflow_results WHERE job_id=run_id;
 source:=job.source; proposal:=result.content->'proposal';
 SELECT i.service_name INTO service FROM incidents i JOIN investigation_runs r
   ON r.incident_id=i.id WHERE r.id=run_id;
 SELECT (m#>>'{content,result,created_at}')::timestamptz INTO floor_at
   FROM jsonb_array_elements(source->'materials') m WHERE m->>'kind'='recovery';
 IF (isfinite(created) AND created>=result.created_at AND isfinite(floor_at)
   AND floor_at<=result.created_at AND result.created_at>=(source->>'created_at')::timestamptz
   AND service IS NOT NULL) IS NOT TRUE THEN
   RAISE EXCEPTION '回流分派时刻、恢复材料或服务来源不完整'; END IF;
 budget:=reflow_budget_snapshot(run_id,created);
 reason:=reflow_basis_blocker(run_id,created);
 IF reason IS NULL AND EXISTS(SELECT 1 FROM jsonb_array_elements(proposal->'tasks') t
   WHERE (t->>'lookback_seconds')::integer>CASE WHEN t->>'role'='change' THEN 21600 ELSE 900 END)
 THEN reason:='reflow_task_window_exceeded'; END IF;
 IF reason IS NULL AND created<=floor_at THEN reason:='reflow_window_not_ready'; END IF;
 IF reason IS NULL THEN reason:=reflow_dispatch_capacity(budget,
   jsonb_array_length(proposal->'tasks'),created); END IF;
 IF reason IS NULL THEN
   round_id:=reflow_dispatch_id(run_id,'reflow-round-v1');
   FOR task IN SELECT value FROM jsonb_array_elements(proposal->'tasks') ORDER BY value->>'task_key'
   LOOP
     tasks:=tasks||jsonb_build_array(jsonb_build_object('task_key',task->>'task_key',
       'role',task->>'role','goal',task->>'goal','service_name',service,
       'window_start',candidate_recovery_utc_text(greatest(floor_at,
         created-make_interval(secs=>(task->>'lookback_seconds')::integer))),
       'window_end',candidate_recovery_utc_text(created),
       'max_attempts',source#>'{coordination_policy,max_attempts_per_task}',
       'evidence_snapshot_id',NULL));
   END LOOP;
   assignment:=jsonb_build_object('round_number',proposal->'round_number',
     'goal',proposal->>'goal','tasks',tasks);
 END IF;
 RETURN jsonb_build_object('version','reinvestigation-dispatch-v1',
   'investigation_run_id',run_id::text,'source_result_sha256',result.content_sha256,
   'source_sha256',job.source_sha256,'source_call_id',result.source_call_id::text,
   'basis_event_cursor',(SELECT max(e.id) FROM incident_events e JOIN investigation_runs r
     ON r.incident_id=e.incident_id WHERE r.id=run_id),
   'service_name',service,'evidence_policy_version','post-verification-read-only-v1',
   'execution_policy_version','reinvestigation-task-v1',
   'observation_floor_at',candidate_recovery_utc_text(floor_at),'budget',budget,
   'status',CASE WHEN reason IS NULL THEN 'queued' ELSE 'blocked' END,'reason_code',reason,
   'round_id',round_id::text,'assignment',assignment,'created_at',candidate_recovery_utc_text(created));
END;
$$ LANGUAGE plpgsql;
"""


_GUARDS = """
CREATE FUNCTION guard_reflow_dispatch() RETURNS trigger AS $$
DECLARE expected JSONB;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '回流分派事实不可更新或删除'; END IF;
 PERFORM 1 FROM work_items WHERE investigation_run_id=NEW.investigation_run_id FOR UPDATE;
 PERFORM 1 FROM investigation_reflow_jobs WHERE id=NEW.investigation_run_id FOR UPDATE;
 PERFORM lock_reflow_basis(NEW.investigation_run_id);
 expected:=reflow_dispatch_content(NEW.investigation_run_id,NEW.created_at);
 IF (NEW.content=expected AND NEW.content_sha256=reflow_hash(expected)
   AND NEW.source_result_sha256=expected->>'source_result_sha256'
   AND NEW.status=expected->>'status' AND NEW.created_at<=clock_timestamp()+interval '1 second'
   AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(expected#>'{budget,calls}') c
     WHERE c->>'status'='reserved')
   AND jsonb_array_length(expected#>'{budget,calls}')=(SELECT count(*) FROM
     investigation_call_reservations WHERE investigation_run_id=NEW.investigation_run_id))
 IS NOT TRUE THEN RAISE EXCEPTION '回流分派必须精确绑定完整来源、当前账本、编译与原因'; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER reflow_dispatch_guard BEFORE INSERT OR UPDATE OR DELETE ON
 investigation_reflow_dispatches FOR EACH ROW EXECUTE FUNCTION guard_reflow_dispatch();

CREATE FUNCTION guard_reflow_dispatch_task() RETURNS trigger AS $$
DECLARE dispatch investigation_reflow_dispatches%ROWTYPE; expected JSONB;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '回流专业任务不可更新或删除'; END IF;
 SELECT * INTO dispatch FROM investigation_reflow_dispatches WHERE
   investigation_run_id=NEW.investigation_run_id;
 SELECT t INTO expected FROM jsonb_array_elements(dispatch.content#>'{assignment,tasks}') t
   WHERE t->>'task_key'=NEW.task_key;
 IF (dispatch.status='queued' AND NEW.id=reflow_dispatch_id(NEW.investigation_run_id,
   'reflow-task-v1:'||NEW.task_key) AND NEW.round_id=(dispatch.content->>'round_id')::uuid
   AND NEW.assignment=expected AND NEW.assignment_sha256=reflow_hash(expected)
   AND NEW.status='pending' AND NEW.created_at=dispatch.created_at) IS NOT TRUE
 THEN RAISE EXCEPTION '回流专业任务必须同事务绑定完整原轮次、身份与精确编译'; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER reflow_dispatch_task_guard BEFORE INSERT OR UPDATE OR DELETE ON
 investigation_reflow_task_queue FOR EACH ROW EXECUTE FUNCTION guard_reflow_dispatch_task();

CREATE FUNCTION validate_reflow_dispatch() RETURNS trigger AS $$
DECLARE target UUID:=NEW.investigation_run_id; dispatch investigation_reflow_dispatches%ROWTYPE;
 total INTEGER; dispatch_tx XID;
BEGIN
 SELECT * INTO dispatch FROM investigation_reflow_dispatches WHERE investigation_run_id=target;
 SELECT xmin INTO dispatch_tx FROM investigation_reflow_dispatches
   WHERE investigation_run_id=target;
 SELECT count(*) INTO total FROM investigation_reflow_task_queue WHERE investigation_run_id=target;
 IF dispatch.status='blocked' THEN
   IF total<>0 THEN RAISE EXCEPTION '被阻断回流不能保留任务'; END IF;
 ELSE
   -- 比较实际写入事务身份，允许整套事实在同一SAVEPOINT创建。
   IF total<>jsonb_array_length(dispatch.content#>'{assignment,tasks}') OR
     EXISTS(SELECT 1 FROM investigation_reflow_task_queue WHERE investigation_run_id=target
       AND xmin IS DISTINCT FROM dispatch_tx)
   THEN RAISE EXCEPTION '回流整轮与完整任务必须同事务创建'; END IF;
   IF reflow_basis_blocker(target,clock_timestamp()) IS NOT NULL THEN
     RAISE EXCEPTION '回流分派提交前原截止、事故或告警已失效'; END IF;
 END IF;
 RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER reflow_dispatch_state AFTER INSERT ON investigation_reflow_dispatches
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_reflow_dispatch();
CREATE CONSTRAINT TRIGGER reflow_dispatch_task_state AFTER INSERT ON investigation_reflow_task_queue
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_reflow_dispatch();
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM investigation_reflow_dispatches) OR
          EXISTS(SELECT 1 FROM investigation_reflow_task_queue)
        THEN RAISE EXCEPTION '不能删除已持久化的回流分派或专业任务'; END IF;
      END $$;
      DROP FUNCTION validate_reflow_dispatch() CASCADE;
      DROP FUNCTION guard_reflow_dispatch_task() CASCADE;
      DROP FUNCTION guard_reflow_dispatch() CASCADE;
      DROP FUNCTION reflow_dispatch_content(UUID,TIMESTAMPTZ);
      DROP FUNCTION reflow_dispatch_capacity(JSONB,INTEGER,TIMESTAMPTZ);
      DROP FUNCTION assert_reflow_dispatch_source(UUID);
      DROP FUNCTION reflow_dispatch_id(UUID,TEXT);
      DROP TABLE investigation_reflow_task_queue;
      DROP TABLE investigation_reflow_dispatches;
    """)
