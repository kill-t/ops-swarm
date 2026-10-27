"""验证失败的有界回流准入、原预算继承及不可变命令闭环。"""

from alembic import op

revision = "0086_reinvestigation_admission"
down_revision = "0085_candidate_convergence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(_COMMAND)
    op.execute(_BUDGET)
    op.execute(_REQUEST)
    op.execute(_GUARDS)


_TABLES = """
CREATE TABLE candidate_reinvestigation_admissions (
 id UUID PRIMARY KEY,
 investigation_run_id UUID NOT NULL UNIQUE REFERENCES
   candidate_incident_convergences(investigation_run_id) ON DELETE RESTRICT,
 incident_id UUID NOT NULL REFERENCES incidents(id) ON DELETE RESTRICT,
 authorization_id UUID NOT NULL UNIQUE REFERENCES operator_authorizations(id) ON DELETE RESTRICT,
 content JSONB NOT NULL,content_sha256 VARCHAR(64) NOT NULL,created_at TIMESTAMPTZ NOT NULL,
 CONSTRAINT ck_reinvestigation_admission_content CHECK
  (jsonb_typeof(content)='object' AND octet_length(content::text)<=524288
   AND content_sha256 ~ '^[0-9a-f]{64}$')
);
CREATE TABLE candidate_reinvestigation_commands (
 cursor BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 authorization_id UUID NOT NULL UNIQUE REFERENCES operator_authorizations(id) ON DELETE RESTRICT,
 investigation_run_id UUID NOT NULL,subject_id VARCHAR(100) NOT NULL,
 command JSONB NOT NULL,raw_body TEXT NOT NULL,command_sha256 VARCHAR(64) NOT NULL,
 outcome VARCHAR(16) NOT NULL,http_status INTEGER NOT NULL,
 request_id UUID REFERENCES candidate_reinvestigation_admissions(id) ON DELETE RESTRICT,
 error_code VARCHAR(100),error_message VARCHAR(1000),created_at TIMESTAMPTZ NOT NULL,
 CONSTRAINT ck_reinvestigation_command_content CHECK
  (jsonb_typeof(command)='object' AND octet_length(command::text)<=32768
   AND octet_length(raw_body) BETWEEN 2 AND 65536 AND command_sha256 ~ '^[0-9a-f]{64}$'
   AND char_length(btrim(subject_id)) BETWEEN 1 AND 100),
 CONSTRAINT ck_reinvestigation_command_outcome CHECK
  ((outcome IN ('applied','replayed') AND request_id IS NOT NULL
    AND error_code IS NULL AND error_message IS NULL AND
    ((outcome='applied' AND http_status=201) OR (outcome='replayed' AND http_status=200)))
   OR (outcome='error' AND request_id IS NULL AND http_status IN (404,409)
    AND error_code IS NOT NULL AND error_message IS NOT NULL))
);
CREATE INDEX ix_reinvestigation_commands_run_cursor ON
 candidate_reinvestigation_commands(investigation_run_id,cursor);
"""


_COMMAND = """
CREATE FUNCTION normalize_reinvestigation_command(raw_body TEXT) RETURNS JSONB AS $$
DECLARE body JSONB; fields TEXT[];
BEGIN
 IF octet_length(convert_to(raw_body,'UTF8')) NOT BETWEEN 2 AND 65536 THEN RETURN NULL; END IF;
 body:=raw_body::jsonb;
 fields:=ARRAY['convergence_sha256','expected_incident_version','idempotency_key','reason'];
 IF (jsonb_typeof(body)='object' AND body ?& fields AND body-(fields||ARRAY['action'])='{}'
   AND (NOT body ? 'action' OR body->'action'='"request_reinvestigation"'::jsonb)
   AND jsonb_typeof(body->'convergence_sha256')='string'
   AND candidate_strip_text(body->>'convergence_sha256') ~ '^[0-9a-f]{64}$'
   AND jsonb_typeof(body->'expected_incident_version')='number'
   AND raw_body::json->>'expected_incident_version' ~ '^[1-9][0-9]*$'
   AND (body->>'expected_incident_version')::numeric<=2147483647
   AND jsonb_typeof(body->'idempotency_key')='string'
   AND char_length(candidate_strip_text(body->>'idempotency_key')) BETWEEN 8 AND 100
   AND candidate_strip_text(body->>'idempotency_key') ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'
   AND jsonb_typeof(body->'reason')='string'
   AND char_length(candidate_strip_text(body->>'reason')) BETWEEN 1 AND 1000) IS NOT TRUE
   THEN RETURN NULL; END IF;
 RETURN jsonb_build_object('action','request_reinvestigation',
   'convergence_sha256',candidate_strip_text(body->>'convergence_sha256'),
   'expected_incident_version',body->'expected_incident_version',
   'idempotency_key',candidate_strip_text(body->>'idempotency_key'),
   'reason',candidate_strip_text(body->>'reason'));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

CREATE FUNCTION assert_reinvestigation_authorization(identifier UUID,subject TEXT,run_id UUID,
 created TIMESTAMPTZ,lock_admission BOOLEAN) RETURNS VOID AS $$
DECLARE admission operator_authorizations%ROWTYPE;
BEGIN
 IF lock_admission THEN SELECT * INTO admission FROM operator_authorizations
   WHERE id=identifier FOR UPDATE;
 ELSE SELECT * INTO admission FROM operator_authorizations WHERE id=identifier; END IF;
 IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
   AND admission.subject_id=subject AND admission.role='engineer'
   AND admission.permissions ? 'manage_incident' AND admission.method='POST'
   AND char_length(admission.credential_id)>0
   AND admission.operation='request_candidate_reinvestigation'
   AND admission.route='/api/v1/investigations/{run_id}/reinvestigation-admission'
   AND admission.resources=jsonb_build_object('run_id',run_id::text)
   AND isfinite(created) AND admission.authorized_at<=created) IS NOT TRUE THEN
   RAISE EXCEPTION '回流准入缺少同源可信授权、主体或路由资源'; END IF;
END;
$$ LANGUAGE plpgsql;
"""


_BUDGET = """
CREATE FUNCTION reinvestigation_model_cost(policy JSONB,inputs BIGINT,outputs BIGINT)
 RETURNS NUMERIC AS $$
 SELECT (ceil((inputs*(policy#>>'{profile,pricing,input_cost_per_million_usd}')::numeric
   +outputs*(policy#>>'{profile,pricing,output_cost_per_million_usd}')::numeric))
   /1000000)::numeric(20,6);
$$ LANGUAGE SQL IMMUTABLE STRICT;

-- 按账本序号与采集时刻同时重建前缀，避免用后来的调用改写已保存的准入依据。
CREATE FUNCTION reinvestigation_budget_snapshot(run_id UUID,created TIMESTAMPTZ)
 RETURNS JSONB AS $$
DECLARE run investigation_runs%ROWTYPE; call investigation_call_reservations%ROWTYPE;
 calls JSONB:='[]'; item JSONB; policy JSONB; model JSONB; allocation JSONB; usage JSONB;
 deadline TIMESTAMPTZ; seq INTEGER:=0; models INTEGER:=0; tools INTEGER:=0;
 inputs BIGINT:=0; outputs BIGINT:=0; reported_inputs BIGINT:=0; reported_outputs BIGINT:=0;
 unknown INTEGER:=0; cost NUMERIC:=0; reported_cost NUMERIC:=0; actual NUMERIC;
 reserved NUMERIC; expected_cost NUMERIC;
BEGIN
 SELECT * INTO run FROM investigation_runs WHERE id=run_id;
 policy:=run.call_budget; model:=run.model_budget;
 IF (run.id IS NOT NULL AND jsonb_typeof(policy)='object'
   AND policy->>'version'='call-count-v1' AND jsonb_typeof(model)='object'
   AND model->>'version'='model-budget-v1' AND isfinite(run.started_at)
   AND run.started_at<=created) IS NOT TRUE THEN
   RAISE EXCEPTION '回流准入原预算策略或开始时刻不完整'; END IF;
 deadline:=run.started_at+make_interval(secs=>(policy->>'max_duration_seconds')::integer);
 FOR call IN SELECT * FROM investigation_call_reservations
   WHERE investigation_run_id=run_id ORDER BY sequence LOOP
   IF call.reserved_at>created THEN CONTINUE; END IF;
   seq:=seq+1;
   IF (call.sequence=seq AND call.reserved_at>=run.started_at AND call.reserved_at<deadline
     AND call.finished_at>=call.reserved_at AND call.finished_at<=created
     AND call.status IN ('succeeded','failed','unknown') AND isfinite(call.finished_at)
     AND ((call.kind='tool' AND call.operation IN ('query_logs','query_metrics','list_changes',
       'read_log_evidence','read_metric_evidence','read_change_evidence','read_log_runbook',
       'read_metric_runbook','read_change_runbook')) OR
       (call.kind='model' AND call.operation IN ('coordinate','analyze_log','analyze_metric',
         'analyze_change','synthesize','review','human_review','evaluate_candidate'))))
     IS NOT TRUE THEN
     RAISE EXCEPTION '回流准入原账本前缀、状态或调用时刻不完整'; END IF;
   allocation:=call.model_allocation; usage:=call.model_usage;
   item:=jsonb_build_object('id',call.id::text,'investigation_run_id',run_id::text,
     'investigation_attempt_id',call.investigation_attempt_id::text,
     'task_id',call.task_id::text,'task_attempt_id',call.task_attempt_id::text,
     'sequence',call.sequence,'invocation_index',call.invocation_index,
     'operation',call.operation,'kind',call.kind,'status',call.status,'error_code',call.error_code,
     'reserved_at',candidate_recovery_utc_text(call.reserved_at),
     'finished_at',candidate_recovery_utc_text(call.finished_at),
     'model_allocation',allocation,'model_usage',usage);
   IF call.human_review_attempt_id IS NOT NULL THEN item:=item||jsonb_build_object(
     'human_review_attempt_id',call.human_review_attempt_id::text); END IF;
   IF call.candidate_attempt_id IS NOT NULL THEN item:=item||jsonb_build_object(
     'candidate_attempt_id',call.candidate_attempt_id::text); END IF;
   calls:=calls||jsonb_build_array(item);
   IF call.kind='tool' THEN
     tools:=tools+1;
     IF allocation IS NOT NULL OR usage IS NOT NULL THEN
       RAISE EXCEPTION '回流准入工具调用不能带模型用量'; END IF;
     CONTINUE;
   END IF;
   models:=models+1;
   expected_cost:=reinvestigation_model_cost(model,
     (allocation->>'estimated_input_tokens')::bigint,
     (allocation->>'output_token_limit')::bigint);
   IF (call.kind='model' AND jsonb_typeof(allocation)='object'
     AND allocation->>'estimator_version'='utf8-json-v1'
     AND (allocation->>'estimated_input_tokens')::integer=(allocation->>'input_bytes')::integer+256
     AND (allocation->>'estimated_input_tokens')::integer<=
       (model->>'max_input_tokens_per_call')::integer
     AND allocation->'output_token_limit'=model->'max_output_tokens_per_call'
     AND (allocation->>'reserved_cost_usd')::numeric=expected_cost) IS NOT TRUE THEN
     RAISE EXCEPTION '回流准入模型分配与原策略不一致'; END IF;
   reserved:=(allocation->>'reserved_cost_usd')::numeric; actual:=0;
   IF usage IS NULL THEN unknown:=unknown+1;
   ELSE
     IF ((usage->>'input_tokens')::bigint>=0 AND (usage->>'output_tokens')::bigint>=0
       AND (usage->>'total_tokens')::bigint=(usage->>'input_tokens')::bigint+
         (usage->>'output_tokens')::bigint) IS NOT TRUE THEN
       RAISE EXCEPTION '回流准入报告模型用量不完整'; END IF;
     actual:=reinvestigation_model_cost(model,(usage->>'input_tokens')::bigint,
       (usage->>'output_tokens')::bigint);
     reported_inputs:=reported_inputs+(usage->>'input_tokens')::bigint;
     reported_outputs:=reported_outputs+(usage->>'output_tokens')::bigint;
     reported_cost:=reported_cost+actual;
   END IF;
   inputs:=inputs+greatest((allocation->>'estimated_input_tokens')::bigint,
     coalesce((usage->>'input_tokens')::bigint,0));
   outputs:=outputs+greatest((allocation->>'output_token_limit')::bigint,
     coalesce((usage->>'output_tokens')::bigint,0));
   cost:=cost+CASE WHEN reserved>=actual THEN reserved ELSE actual END;
 END LOOP;
 RETURN jsonb_build_object('investigation_run_id',run_id::text,'policy',policy,
   'started_at',candidate_recovery_utc_text(run.started_at),
   'deadline',candidate_recovery_utc_text(deadline),'reserved_model_calls',models,
   'reserved_tool_calls',tools,'calls',calls,'model_budget',model,
   'model_usage',jsonb_build_object('committed_input_tokens',inputs,
     'committed_output_tokens',outputs,'committed_cost_usd',cost::text,
     'reported_input_tokens',reported_inputs,'reported_output_tokens',reported_outputs,
     'reported_cost_usd',CASE WHEN unknown=0 THEN reported_cost::text ELSE NULL END,
     'unknown_model_calls',unknown));
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION reinvestigation_budget_blocker(budget JSONB,created TIMESTAMPTZ)
 RETURNS TEXT AS $$
DECLARE policy JSONB:=budget->'policy'; model JSONB:=budget->'model_budget';
 usage JSONB:=budget->'model_usage';
BEGIN
 IF created>=candidate_release_timestamp(budget->'deadline') THEN
   RETURN 'run_deadline_exceeded'; END IF;
 IF (budget->>'reserved_model_calls')::integer+1>(policy->>'max_model_calls')::integer THEN
   RETURN 'model_call_budget_exceeded'; END IF;
 IF (budget->>'reserved_tool_calls')::integer>(policy->>'max_tool_calls')::integer
   OR EXISTS (SELECT 1 FROM jsonb_array_elements(budget->'calls') call
     WHERE call->>'kind'='tool' GROUP BY call->>'task_id'
     HAVING count(*)>(policy->>'max_tool_calls_per_task')::integer) THEN
   RETURN 'tool_call_budget_exceeded'; END IF;
 IF (usage->>'committed_input_tokens')::bigint+(model->>'max_input_tokens_per_call')::bigint
   >(model->>'max_input_tokens')::bigint THEN RETURN 'run_input_budget_exceeded'; END IF;
 IF (usage->>'committed_output_tokens')::bigint+(model->>'max_output_tokens_per_call')::bigint
   >(model->>'max_output_tokens')::bigint THEN RETURN 'run_output_budget_exceeded'; END IF;
 IF (usage->>'committed_cost_usd')::numeric+reinvestigation_model_cost(model,
   (model->>'max_input_tokens_per_call')::bigint,(model->>'max_output_tokens_per_call')::bigint)
   >(model->>'max_estimated_cost_usd')::numeric THEN RETURN 'model_cost_budget_exceeded'; END IF;
 IF EXISTS (SELECT 1 FROM jsonb_array_elements(budget->'calls') call
   WHERE call->>'kind'='model' AND call->'model_usage'<>'null'::jsonb
     AND ((call#>>'{model_usage,input_tokens}')::bigint>
       (call#>>'{model_allocation,estimated_input_tokens}')::bigint
     OR (call#>>'{model_usage,output_tokens}')::bigint>
       (call#>>'{model_allocation,output_token_limit}')::bigint
     OR reinvestigation_model_cost(model,(call#>>'{model_usage,input_tokens}')::bigint,
       (call#>>'{model_usage,output_tokens}')::bigint)>
       (call#>>'{model_allocation,reserved_cost_usd}')::numeric)) THEN
   RETURN 'model_usage_exceeded_allocation'; END IF;
 RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


_REQUEST = """
CREATE FUNCTION lock_reinvestigation_parent(run_id UUID) RETURNS VOID AS $$
DECLARE candidate UUID;
BEGIN
 PERFORM 1 FROM work_items WHERE investigation_run_id=run_id FOR UPDATE;
 PERFORM 1 FROM investigation_runs WHERE id=run_id FOR UPDATE;
 SELECT candidate_job_id INTO candidate FROM investigation_candidate_settlements
   WHERE investigation_run_id=run_id;
 PERFORM 1 FROM investigation_candidate_evaluation_jobs WHERE id=candidate FOR UPDATE;
 PERFORM 1 FROM incidents WHERE id=(SELECT incident_id FROM investigation_runs WHERE id=run_id)
   FOR UPDATE;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION reinvestigation_has_new_alert(run_id UUID) RETURNS BOOLEAN AS $$
 SELECT EXISTS (SELECT 1 FROM incident_events event
   JOIN candidate_incident_convergences source ON source.incident_id=event.incident_id
   WHERE source.investigation_run_id=run_id AND event.event_type='incident.alert_attached'
   AND event.id>(SELECT max(original.id) FROM incident_events original
     WHERE original.incident_id=source.incident_id
       AND original.actor_id='candidate-convergence:'||run_id::text
       AND original.incident_version=(source.content->>'after_version')::integer
       AND original.event_type='incident.status_changed'));
$$ LANGUAGE SQL STABLE;

CREATE FUNCTION assert_reinvestigation_request(item candidate_reinvestigation_admissions,
 require_audit BOOLEAN,require_current BOOLEAN) RETURNS VOID AS $$
DECLARE source candidate_incident_convergences%ROWTYPE; run investigation_runs%ROWTYPE;
 incident incidents%ROWTYPE; audit candidate_reinvestigation_commands%ROWTYPE;
 admission operator_authorizations%ROWTYPE; body JSONB; expected JSONB; budget JSONB;
 command JSONB; blocker TEXT;
BEGIN
 SELECT * INTO run FROM investigation_runs WHERE id=item.investigation_run_id;
 SELECT * INTO source FROM candidate_incident_convergences
   WHERE investigation_run_id=item.investigation_run_id;
 IF source.investigation_run_id IS NULL THEN RAISE EXCEPTION '回流准入缺少130收敛来源'; END IF;
 PERFORM assert_candidate_convergence_history(source);
 body:=item.content; command:=body->'command';
 PERFORM assert_reinvestigation_authorization(item.authorization_id,body->>'subject_id',
   item.investigation_run_id,item.created_at,FALSE);
 IF (item.id=run.id AND source.incident_id=item.incident_id AND run.incident_id=item.incident_id
   AND run.status='settled' AND run.candidate_slot_released_at IS NULL
   AND source.content->>'outcome'='observed_failure'
   AND source.content->'slot_released'='false'::jsonb
   AND source.content->>'after_status'='investigating'
   AND item.created_at>=source.created_at AND isfinite(item.created_at)
   AND normalize_reinvestigation_command(candidate_canonical_json(command))=command
   AND command->>'convergence_sha256'=source.content_sha256
   AND command->'expected_incident_version'=source.content->'after_version') IS NOT TRUE THEN
   RAISE EXCEPTION '回流准入需要完整失败来源、原持槽运行和精确命令'; END IF;
 budget:=reinvestigation_budget_snapshot(run.id,item.created_at);
 blocker:=reinvestigation_budget_blocker(budget,item.created_at);
 IF blocker IS NOT NULL THEN RAISE EXCEPTION '回流准入原预算不足: %',blocker; END IF;
 expected:=jsonb_build_object('id',run.id::text,'investigation_run_id',run.id::text,
   'incident_id',item.incident_id::text,'root_run_id',run.id::text,
   'previous_run_id',run.id::text,'budget_run_id',run.id::text,
   'convergence_content_sha256',source.content_sha256,
   'incident_version',source.content->'after_version',
   'policy_version','candidate-reinvestigation-admission-v1','max_reflows',1,'reflow_number',1,
   'dispatch_status','not_dispatched','budget',budget,'subject_id',body->>'subject_id',
   'authorization_id',item.authorization_id::text,'command',command,
   'command_sha256',encode(sha256(convert_to(candidate_canonical_json(command),'UTF8')),'hex'),
   'created_at',candidate_recovery_utc_text(item.created_at));
 IF (body=expected AND item.content_sha256=encode(sha256(convert_to(
   candidate_canonical_json(expected),'UTF8')),'hex')) IS NOT TRUE THEN
   RAISE EXCEPTION '回流准入正文、原预算前缀或内容摘要无法重建'; END IF;
 IF require_current THEN
   SELECT * INTO incident FROM incidents WHERE id=item.incident_id;
   IF (incident.status='investigating'
     AND to_jsonb(incident.version)=source.content->'after_version'
     AND NOT reinvestigation_has_new_alert(run.id)
     AND item.created_at>=incident.updated_at
     AND item.created_at<=clock_timestamp()+interval '1 second'
     AND clock_timestamp()<candidate_release_timestamp(budget->'deadline')
     AND NOT EXISTS (SELECT 1 FROM investigation_call_reservations
       WHERE investigation_run_id=run.id AND reserved_at>item.created_at)) IS NOT TRUE THEN
     RAISE EXCEPTION '回流准入必须绑定当前事故、原期限和完整原账本'; END IF;
 END IF;
 IF require_audit THEN
   SELECT * INTO audit FROM candidate_reinvestigation_commands
     WHERE authorization_id=item.authorization_id;
   SELECT * INTO admission FROM operator_authorizations WHERE id=item.authorization_id;
   IF (audit.outcome='applied' AND audit.http_status=201 AND audit.request_id=item.id
     AND audit.investigation_run_id=item.investigation_run_id
     AND audit.subject_id=body->>'subject_id' AND audit.command=command
     AND audit.command_sha256=body->>'command_sha256' AND audit.created_at=item.created_at
     AND audit.error_code IS NULL AND audit.error_message IS NULL
     AND normalize_reinvestigation_command(audit.raw_body)=audit.command
     AND audit.command_sha256=encode(sha256(convert_to(
       candidate_canonical_json(audit.command),'UTF8')),'hex')
     AND admission.body_sha256=encode(sha256(convert_to(audit.raw_body,'UTF8')),'hex'))
     IS NOT TRUE THEN RAISE EXCEPTION '回流准入缺少精确成功命令历史'; END IF;
 END IF;
END;
$$ LANGUAGE plpgsql;

-- 返回正常业务拒绝原因；损坏的权威来源继续抛错，不能伪装为普通失败审计。
CREATE FUNCTION reinvestigation_command_error(run_id UUID,subject TEXT,command JSONB,
 created TIMESTAMPTZ) RETURNS TEXT AS $$
DECLARE run investigation_runs%ROWTYPE; source candidate_incident_convergences%ROWTYPE;
 item candidate_reinvestigation_admissions%ROWTYPE; incident incidents%ROWTYPE; budget JSONB;
BEGIN
 SELECT * INTO run FROM investigation_runs WHERE id=run_id;
 IF run.id IS NULL THEN RETURN 'reinvestigation_run_not_found'; END IF;
 SELECT * INTO source FROM candidate_incident_convergences WHERE investigation_run_id=run_id;
 IF source.investigation_run_id IS NULL THEN RETURN 'reinvestigation_source_not_found'; END IF;
 PERFORM assert_candidate_convergence_history(source);
 SELECT * INTO item FROM candidate_reinvestigation_admissions WHERE investigation_run_id=run_id;
 IF item.id IS NOT NULL THEN
   PERFORM assert_reinvestigation_request(item,TRUE,FALSE);
   IF item.content->>'subject_id'=subject
     AND item.content->'command'->>'idempotency_key'=command->>'idempotency_key' THEN
     IF item.content->'command'=command THEN RETURN NULL; END IF;
     RETURN 'reinvestigation_idempotency_conflict';
   END IF;
   RETURN 'reinvestigation_already_exists';
 END IF;
 IF source.content->>'outcome'<>'observed_failure'
   OR source.content->'slot_released'<>'false'::jsonb
   OR run.candidate_slot_released_at IS NOT NULL OR run.status<>'settled' THEN
   RETURN 'reinvestigation_source_not_eligible'; END IF;
 SELECT * INTO incident FROM incidents WHERE id=run.incident_id;
 IF (command->>'convergence_sha256'=source.content_sha256
   AND command->'expected_incident_version'=source.content->'after_version'
   AND incident.status='investigating'
   AND to_jsonb(incident.version)=source.content->'after_version'
   AND NOT reinvestigation_has_new_alert(run_id)) IS NOT TRUE THEN
   RETURN 'reinvestigation_basis_changed'; END IF;
 IF created>=run.started_at+make_interval(
   secs=>(run.call_budget->>'max_duration_seconds')::integer) THEN
   RETURN 'run_deadline_exceeded'; END IF;
 IF EXISTS (SELECT 1 FROM investigation_call_reservations
   WHERE investigation_run_id=run_id AND status='reserved') THEN RETURN 'calls_in_flight'; END IF;
 budget:=reinvestigation_budget_snapshot(run_id,created);
 RETURN reinvestigation_budget_blocker(budget,created);
END;
$$ LANGUAGE plpgsql;
"""


_GUARDS = """
CREATE FUNCTION guard_reinvestigation_request() RETURNS trigger AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '回流准入请求不可更新或删除'; END IF;
 PERFORM assert_reinvestigation_authorization(NEW.authorization_id,NEW.content->>'subject_id',
   NEW.investigation_run_id,NEW.created_at,TRUE);
 PERFORM lock_reinvestigation_parent(NEW.investigation_run_id);
 PERFORM assert_reinvestigation_request(NEW,FALSE,TRUE);
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER reinvestigation_request_guard BEFORE INSERT OR UPDATE OR DELETE
 ON candidate_reinvestigation_admissions FOR EACH ROW
 EXECUTE FUNCTION guard_reinvestigation_request();

CREATE FUNCTION assert_reinvestigation_command(item candidate_reinvestigation_commands)
 RETURNS VOID AS $$
DECLARE admission operator_authorizations%ROWTYPE;
 request candidate_reinvestigation_admissions%ROWTYPE;
 reason TEXT;
BEGIN
 PERFORM assert_reinvestigation_authorization(item.authorization_id,item.subject_id,
   item.investigation_run_id,item.created_at,FALSE);
 SELECT * INTO admission FROM operator_authorizations WHERE id=item.authorization_id;
 IF (admission.body_sha256=encode(sha256(convert_to(item.raw_body,'UTF8')),'hex')
   AND normalize_reinvestigation_command(item.raw_body)=item.command
   AND item.command_sha256=encode(sha256(convert_to(
     candidate_canonical_json(item.command),'UTF8')),'hex')) IS NOT TRUE THEN
   RAISE EXCEPTION '回流准入原始正文、规范命令或摘要不一致'; END IF;
 IF item.outcome='error' THEN
   reason:=reinvestigation_command_error(item.investigation_run_id,item.subject_id,
     item.command,item.created_at);
   IF (reason IS NOT NULL AND item.error_code=reason
     AND char_length(candidate_strip_text(item.error_message)) BETWEEN 1 AND 1000
     AND item.http_status=CASE WHEN reason IN
       ('reinvestigation_run_not_found','reinvestigation_source_not_found') THEN 404 ELSE 409 END)
     IS NOT TRUE THEN RAISE EXCEPTION '回流准入失败审计缺少合理业务拒绝'; END IF;
   RETURN;
 END IF;
 SELECT * INTO request FROM candidate_reinvestigation_admissions WHERE id=item.request_id;
 IF (request.id IS NOT NULL AND request.investigation_run_id=item.investigation_run_id
   AND request.content->>'subject_id'=item.subject_id AND request.content->'command'=item.command
   AND request.content->>'command_sha256'=item.command_sha256
   AND item.created_at>=request.created_at
   AND ((item.outcome='applied' AND item.http_status=201
     AND request.authorization_id=item.authorization_id AND request.created_at=item.created_at)
     OR (item.outcome='replayed' AND item.http_status=200
       AND request.authorization_id<>item.authorization_id))) IS NOT TRUE THEN
   RAISE EXCEPTION '回流准入审计缺少精确绑定成功请求'; END IF;
 PERFORM assert_reinvestigation_request(request,item.outcome='replayed',FALSE);
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION guard_reinvestigation_command() RETURNS trigger AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '回流准入命令审计不可更新或删除'; END IF;
 PERFORM assert_reinvestigation_authorization(NEW.authorization_id,NEW.subject_id,
   NEW.investigation_run_id,NEW.created_at,TRUE);
 PERFORM lock_reinvestigation_parent(NEW.investigation_run_id);
 IF NEW.created_at>clock_timestamp()+interval '1 second' THEN
   RAISE EXCEPTION '回流准入审计不能保存未来时刻'; END IF;
 PERFORM assert_reinvestigation_command(NEW);
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER reinvestigation_command_guard BEFORE INSERT OR UPDATE OR DELETE
 ON candidate_reinvestigation_commands FOR EACH ROW
 EXECUTE FUNCTION guard_reinvestigation_command();

CREATE FUNCTION validate_reinvestigation_state() RETURNS trigger AS $$
DECLARE item candidate_reinvestigation_admissions%ROWTYPE;
 audit candidate_reinvestigation_commands%ROWTYPE; fact_tx XID; audit_tx XID;
BEGIN
 IF TG_TABLE_NAME='candidate_reinvestigation_admissions' THEN
   SELECT * INTO item FROM candidate_reinvestigation_admissions WHERE id=NEW.id;
 ELSIF NEW.outcome='applied' THEN
   SELECT * INTO item FROM candidate_reinvestigation_admissions WHERE id=NEW.request_id;
 ELSE RETURN NULL; END IF;
 PERFORM assert_reinvestigation_request(item,TRUE,TRUE);
 SELECT * INTO audit FROM candidate_reinvestigation_commands
   WHERE authorization_id=item.authorization_id;
 SELECT xmin INTO fact_tx FROM candidate_reinvestigation_admissions WHERE id=item.id;
 SELECT xmin INTO audit_tx FROM candidate_reinvestigation_commands WHERE cursor=audit.cursor;
 IF fact_tx IS DISTINCT FROM audit_tx THEN
   RAISE EXCEPTION '回流准入请求与成功审计必须同事务保存'; END IF;
 RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER reinvestigation_request_state AFTER INSERT
 ON candidate_reinvestigation_admissions DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION validate_reinvestigation_state();
CREATE CONSTRAINT TRIGGER reinvestigation_command_state AFTER INSERT
 ON candidate_reinvestigation_commands DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION validate_reinvestigation_state();

CREATE FUNCTION guard_reinvestigation_call_prefix() RETURNS trigger AS $$
DECLARE item candidate_reinvestigation_admissions%ROWTYPE;
BEGIN
 IF TG_OP<>'INSERT' THEN
   PERFORM 1 FROM work_items WHERE investigation_run_id=OLD.investigation_run_id FOR UPDATE;
   SELECT * INTO item FROM candidate_reinvestigation_admissions
     WHERE investigation_run_id=OLD.investigation_run_id;
   IF item.id IS NOT NULL AND OLD.sequence<=jsonb_array_length(item.content->'budget'->'calls')
     AND (TG_OP='DELETE' OR to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD)) THEN
     RAISE EXCEPTION '回流准入原账本前缀不可改写或删除'; END IF;
   IF TG_OP='DELETE' THEN RETURN OLD; END IF;
   IF item.id IS NOT NULL AND OLD.sequence<=jsonb_array_length(item.content->'budget'->'calls')
     THEN RETURN NEW; END IF;
 END IF;
 PERFORM 1 FROM work_items WHERE investigation_run_id=NEW.investigation_run_id FOR UPDATE;
 SELECT * INTO item FROM candidate_reinvestigation_admissions
   WHERE investigation_run_id=NEW.investigation_run_id;
 IF item.id IS NOT NULL AND (NEW.reserved_at<=item.created_at
   OR NEW.sequence<=jsonb_array_length(item.content->'budget'->'calls')) THEN
   RAISE EXCEPTION '回流准入原账本前缀不可改写，新增调用必须晚于准入'; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER reinvestigation_call_prefix_guard BEFORE INSERT OR UPDATE OR DELETE
 ON investigation_call_reservations FOR EACH ROW
 EXECUTE FUNCTION guard_reinvestigation_call_prefix();
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM candidate_reinvestigation_admissions)
          OR EXISTS (SELECT 1 FROM candidate_reinvestigation_commands) THEN
          RAISE EXCEPTION '不能删除已保存的回流准入请求或命令审计'; END IF;
      END $$;
      DROP FUNCTION guard_reinvestigation_call_prefix() CASCADE;
      DROP FUNCTION validate_reinvestigation_state() CASCADE;
      DROP FUNCTION guard_reinvestigation_command() CASCADE;
      DROP FUNCTION assert_reinvestigation_command(candidate_reinvestigation_commands);
      DROP FUNCTION guard_reinvestigation_request() CASCADE;
      DROP FUNCTION reinvestigation_command_error(UUID,TEXT,JSONB,TIMESTAMPTZ);
      DROP FUNCTION
        assert_reinvestigation_request(candidate_reinvestigation_admissions,BOOLEAN,BOOLEAN);
      DROP FUNCTION reinvestigation_has_new_alert(UUID);
      DROP FUNCTION lock_reinvestigation_parent(UUID);
      DROP FUNCTION reinvestigation_budget_blocker(JSONB,TIMESTAMPTZ);
      DROP FUNCTION reinvestigation_budget_snapshot(UUID,TIMESTAMPTZ);
      DROP FUNCTION reinvestigation_model_cost(JSONB,BIGINT,BIGINT);
      DROP FUNCTION assert_reinvestigation_authorization(UUID,TEXT,UUID,TIMESTAMPTZ,BOOLEAN);
      DROP FUNCTION normalize_reinvestigation_command(TEXT);
      DROP TABLE candidate_reinvestigation_commands;
      DROP TABLE candidate_reinvestigation_admissions;
    """)
