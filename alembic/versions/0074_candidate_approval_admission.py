"""候选草案的不可变可信审批请求与事故迁移闭环。"""

from alembic import op

revision = "0074_candidate_approval"
down_revision = "0073_candidate_plan_admission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(_COMMANDS)
    op.execute(_HISTORY)
    op.execute(_REQUESTS)
    op.execute(_STATE)


_TABLES = """
CREATE TABLE candidate_approval_requests (
  id UUID NOT NULL PRIMARY KEY,
  investigation_run_id UUID NOT NULL,
  candidate_job_id UUID NOT NULL,
  plan_id UUID NOT NULL,
  plan_content_sha256 VARCHAR(64) NOT NULL,
  wait_version INTEGER,
  previous_incident_version INTEGER NOT NULL,
  incident_version INTEGER NOT NULL,
  event_cursor BIGINT NOT NULL REFERENCES incident_events(id) ON DELETE RESTRICT,
  policy_version VARCHAR(40) NOT NULL,
  status VARCHAR(16) NOT NULL,
  requested_by_id VARCHAR(100) NOT NULL,
  authorization_id UUID NOT NULL REFERENCES operator_authorizations(id) ON DELETE RESTRICT,
  idempotency_key VARCHAR(100) NOT NULL,
  command_sha256 VARCHAR(64) NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
  CONSTRAINT fk_candidate_approval_plan_run FOREIGN KEY(plan_id,investigation_run_id)
    REFERENCES candidate_action_plans(id,investigation_run_id) ON DELETE RESTRICT,
  CONSTRAINT fk_candidate_approval_job_run FOREIGN KEY(candidate_job_id,investigation_run_id)
    REFERENCES investigation_candidate_evaluation_jobs(id,investigation_run_id) ON DELETE RESTRICT,
  CONSTRAINT uq_candidate_approval_plan UNIQUE(plan_id),
  CONSTRAINT uq_candidate_approval_authorization UNIQUE(authorization_id),
  CONSTRAINT uq_candidate_approval_event UNIQUE(event_cursor),
  CONSTRAINT uq_candidate_approval_key UNIQUE(investigation_run_id,requested_by_id,idempotency_key),
  CONSTRAINT ck_candidate_approval_policy CHECK (policy_version='candidate-approval-v1'),
  CONSTRAINT ck_candidate_approval_status CHECK (status='pending'),
  CONSTRAINT ck_candidate_approval_wait CHECK
    (wait_version IS NULL OR wait_version BETWEEN 2 AND 16),
  CONSTRAINT ck_candidate_approval_versions CHECK
    (previous_incident_version>=1 AND incident_version=previous_incident_version+1),
  CONSTRAINT ck_candidate_approval_hashes CHECK (plan_content_sha256 ~ '^[0-9a-f]{64}$'
    AND command_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_candidate_approval_subject CHECK
    (char_length(btrim(requested_by_id)) BETWEEN 1 AND 100),
  CONSTRAINT ck_candidate_approval_key CHECK (char_length(idempotency_key) BETWEEN 8 AND 100
    AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'),
  CONSTRAINT ck_candidate_approval_expiry CHECK
    (expires_at>created_at AND expires_at<=created_at+interval '30 minutes')
);
CREATE TABLE candidate_approval_commands (
  cursor BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  authorization_id UUID NOT NULL REFERENCES operator_authorizations(id) ON DELETE RESTRICT,
  investigation_run_id UUID NOT NULL,
  candidate_job_id UUID NOT NULL,
  subject_id VARCHAR(100) NOT NULL,
  command JSONB NOT NULL,
  raw_body TEXT NOT NULL,
  command_sha256 VARCHAR(64) NOT NULL,
  outcome VARCHAR(16) NOT NULL,
  http_status INTEGER NOT NULL,
  request_id UUID REFERENCES candidate_approval_requests(id) ON DELETE RESTRICT,
  error_code VARCHAR(100),
  error_message VARCHAR(1000),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  CONSTRAINT uq_candidate_approval_commands_authorization UNIQUE(authorization_id),
  CONSTRAINT ck_candidate_approval_commands_body CHECK
    (jsonb_typeof(command)='object' AND octet_length(command::text)<=32768),
  CONSTRAINT ck_candidate_approval_commands_raw CHECK
    (octet_length(raw_body) BETWEEN 2 AND 65536),
  CONSTRAINT ck_candidate_approval_commands_hash CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_candidate_approval_commands_subject CHECK
    (char_length(btrim(subject_id)) BETWEEN 1 AND 100),
  CONSTRAINT ck_candidate_approval_commands_outcome CHECK
    ((outcome IN ('applied','replayed') AND request_id IS NOT NULL AND error_code IS NULL
      AND error_message IS NULL AND ((outcome='applied' AND http_status=201)
      OR (outcome='replayed' AND http_status=200)))
    OR (outcome='error' AND request_id IS NULL AND error_code IS NOT NULL
      AND error_message IS NOT NULL AND http_status IN (404,409))),
  CONSTRAINT ck_candidate_approval_commands_error CHECK
    (error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,100}$')
);
CREATE INDEX ix_candidate_approval_commands_run_cursor
  ON candidate_approval_commands(investigation_run_id,cursor);
"""

_COMMANDS = """
CREATE FUNCTION normalize_candidate_approval_command(raw_body TEXT) RETURNS JSONB AS $$
DECLARE body JSONB;
BEGIN
  body:=raw_body::jsonb;
  IF (jsonb_typeof(body)='object'
    AND body ?& ARRAY['plan_content_sha256','expected_wait_version',
                     'expected_incident_version','acknowledge_unresolved_conditions','idempotency_key','reason']
    AND (body-ARRAY['action','plan_content_sha256','expected_wait_version',
                   'expected_incident_version','acknowledge_unresolved_conditions','idempotency_key','reason'])='{}'::jsonb
    AND (NOT body ? 'action' OR (jsonb_typeof(body->'action')='string'
                                AND body->>'action'='request_approval'))
    AND jsonb_typeof(body->'plan_content_sha256')='string'
    AND jsonb_typeof(body->'idempotency_key')='string'
    AND jsonb_typeof(body->'reason')='string'
    AND (body->'expected_wait_version'='null'::jsonb OR
      (jsonb_typeof(body->'expected_wait_version')='number'
        AND raw_body::json->>'expected_wait_version' ~ '^[0-9]+$'
        AND body->'expected_wait_version' IN
          (SELECT to_jsonb(value) FROM generate_series(2,16) value)))
    AND body->'acknowledge_unresolved_conditions'='true'::jsonb
    AND jsonb_typeof(body->'expected_incident_version')='number'
    AND raw_body::json->>'expected_incident_version' ~ '^[0-9]+$'
    AND (body->>'expected_incident_version')::numeric>=1) IS NOT TRUE THEN
    RETURN NULL;
  END IF;
  RETURN jsonb_build_object('action','request_approval',
    'plan_content_sha256',candidate_strip_text(body->>'plan_content_sha256'),
    'expected_wait_version',body->'expected_wait_version',
    'expected_incident_version',body->'expected_incident_version',
    'acknowledge_unresolved_conditions',TRUE,
    'idempotency_key',candidate_strip_text(body->>'idempotency_key'),
    'reason',candidate_strip_text(body->>'reason'));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
  RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

CREATE FUNCTION guard_candidate_approval_command() RETURNS trigger AS $$
DECLARE
  admission operator_authorizations%ROWTYPE;
  request candidate_approval_requests%ROWTYPE;
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选审批命令审计不可更新或删除'; END IF;
  SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
  IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
    AND admission.role='engineer'
    AND admission.permissions ?& ARRAY['request_approval','manage_incident']
    AND admission.subject_id=NEW.subject_id AND admission.method='POST'
    AND admission.operation='create_candidate_approval_request'
    AND admission.route=
      '/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan/approval-request'
    AND admission.resources=jsonb_build_object('run_id',NEW.investigation_run_id::text,
                                              'job_id',NEW.candidate_job_id::text)
    AND admission.authorized_at<=NEW.created_at
    AND admission.body_sha256=encode(sha256(convert_to(NEW.raw_body,'UTF8')),'hex'))
    IS NOT TRUE THEN
    RAISE EXCEPTION '候选审批命令缺少精确原始正文或同源可信准入';
  END IF;
  IF (NEW.command->>'action'='request_approval'
    AND NEW.command->>'plan_content_sha256' ~ '^[0-9a-f]{64}$'
    AND char_length(NEW.command->>'idempotency_key') BETWEEN 8 AND 100
    AND NEW.command->>'idempotency_key' ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'
    AND char_length(candidate_strip_text(NEW.command->>'reason')) BETWEEN 1 AND 500
    AND NEW.command_sha256=encode(sha256(convert_to(
        candidate_canonical_json(NEW.command),'UTF8')),'hex')) IS NOT TRUE THEN
    RAISE EXCEPTION '候选审批命令缺少有界精确规范来源绑定';
  END IF;
  IF normalize_candidate_approval_command(NEW.raw_body) IS DISTINCT FROM NEW.command THEN
    RAISE EXCEPTION '候选审批命令原始正文与规范命令不一致';
  END IF;
  IF NEW.outcome IN ('applied','replayed') THEN
    SELECT * INTO request FROM candidate_approval_requests WHERE id=NEW.request_id;
    IF (request.id IS NOT NULL AND request.investigation_run_id=NEW.investigation_run_id
      AND request.candidate_job_id=NEW.candidate_job_id AND request.requested_by_id=NEW.subject_id
      AND request.command_sha256=NEW.command_sha256
      AND request.plan_content_sha256=NEW.command->>'plan_content_sha256'
      AND COALESCE(to_jsonb(request.wait_version),'null'::jsonb)=
          NEW.command->'expected_wait_version'
      AND to_jsonb(request.previous_incident_version)=NEW.command->'expected_incident_version'
      AND request.idempotency_key=NEW.command->>'idempotency_key'
      AND request.created_at<=NEW.created_at
      AND ((NEW.outcome='applied' AND request.authorization_id=NEW.authorization_id
            AND request.created_at=NEW.created_at)
        OR (NEW.outcome='replayed' AND request.authorization_id<>NEW.authorization_id))) IS NOT TRUE
    THEN RAISE EXCEPTION '候选审批命令结果与原始审批请求不一致'; END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_approval_commands_guard BEFORE INSERT OR UPDATE OR DELETE
  ON candidate_approval_commands FOR EACH ROW EXECUTE FUNCTION guard_candidate_approval_command();
"""

_HISTORY = """
CREATE FUNCTION assert_candidate_approval_plan_history(plan candidate_action_plans)
RETURNS VOID AS $$
DECLARE
  job investigation_candidate_evaluation_jobs%ROWTYPE;
  result investigation_candidate_evaluation_results%ROWTYPE;
  attempt investigation_candidate_evaluation_attempts%ROWTYPE;
  caller investigation_candidate_evaluation_attempts%ROWTYPE;
  source_call investigation_call_reservations%ROWTYPE;
  run investigation_runs%ROWTYPE;
  admission operator_authorizations%ROWTYPE;
  incident incidents%ROWTYPE;
  expected_content JSONB;
  input_occupied NUMERIC;
  output_occupied NUMERIC;
  cost_occupied NUMERIC;
  input_price NUMERIC;
  output_price NUMERIC;
BEGIN
  PERFORM 1 FROM work_items WHERE investigation_run_id=plan.investigation_run_id FOR UPDATE;
  SELECT * INTO job FROM investigation_candidate_evaluation_jobs
    WHERE id=plan.candidate_job_id FOR UPDATE;
  SELECT * INTO run FROM investigation_runs WHERE id=plan.investigation_run_id;
  SELECT * INTO incident FROM incidents WHERE id=run.incident_id FOR UPDATE;
  IF (job.id IS NOT NULL AND job.investigation_run_id=run.id
    AND incident.id IS NOT NULL AND job.status='succeeded'
    AND job.wait_version IS NOT DISTINCT FROM plan.wait_version) IS NOT TRUE THEN
    RAISE EXCEPTION '候选草案需要同运行成功候选、当前等待及事故版本';
  END IF;
  PERFORM assert_candidate_source(job,FALSE);
  SELECT * INTO result FROM investigation_candidate_evaluation_results WHERE job_id=job.id;
  SELECT * INTO attempt FROM investigation_candidate_evaluation_attempts WHERE id=result.attempt_id;
  SELECT * INTO source_call FROM investigation_call_reservations WHERE id=result.source_call_id;
  SELECT * INTO caller FROM investigation_candidate_evaluation_attempts
    WHERE id=source_call.candidate_attempt_id;
  IF (result.job_id=job.id AND attempt.job_id=job.id AND attempt.status='succeeded'
    AND attempt.investigation_run_id=run.id AND attempt.number=job.attempt_count
    AND attempt.finished_at=result.created_at AND job.completed_at=result.created_at
    AND result.created_at>=attempt.started_at AND result.created_at<attempt.lease_expires_at
    AND result.created_at<=plan.created_at
    AND source_call.id IS NOT NULL AND caller.job_id=job.id
    AND caller.investigation_run_id=run.id AND source_call.investigation_run_id=run.id
    AND source_call.investigation_attempt_id=
      (job.source #>> '{handoff,investigation_attempt_id}')::uuid
    AND source_call.operation='evaluate_candidate' AND source_call.kind='model'
    AND source_call.status='succeeded' AND source_call.invocation_index=0
    AND source_call.task_id IS NULL AND source_call.task_attempt_id IS NULL
    AND source_call.human_review_attempt_id IS NULL
    AND source_call.idempotency_key='candidate:'||caller.id::text
    AND caller.started_at<=source_call.reserved_at
    AND source_call.reserved_at<=source_call.finished_at
    AND source_call.finished_at<caller.lease_expires_at
    AND source_call.finished_at<=result.created_at
    AND source_call.model_input=job.packet->'model_input'
    AND source_call.model_allocation=job.packet->'allocation'
    AND source_call.context_manifest=job.packet->'manifest'
    AND source_call.call_result=jsonb_build_object('kind','model',
      'version','candidate-model-result-v1','proposal',result.content->'proposal')
    AND result.content->>'version'='candidate-evaluation-result-v1'
    AND result.content->>'disposition'='planning_review'
    AND result.content->>'reason_code'='candidate_ready_for_planning_review'
    AND result.content->'execution_eligible'='false'::jsonb
    AND plan.candidate_result_sha256=result.content_sha256
    AND result.content_sha256=encode(sha256(convert_to(
        candidate_canonical_json(result.content),'UTF8')),'hex')
    AND job.packet_sha256=encode(sha256(convert_to(
        candidate_canonical_json(job.packet),'UTF8')),'hex')) IS NOT TRUE THEN
    RAISE EXCEPTION '候选草案缺少完整候选结果、成功调用或真实执行归属';
  END IF;
  -- 完整来源含工具浮点观测，JSONB 与 Python 的指数记法不同；其散列由应用重建。
  -- 原生通过 assert_candidate_source 核对实际来源链，不能用文本重算误拒合法观测。
  PERFORM assert_candidate_step(result.content,job.source);
  expected_content:=jsonb_build_object('version','candidate-plan-content-v1',
    'candidate_job_id',job.id::text,'candidate_result_sha256',result.content_sha256,
    'open_hypothesis_id',job.source #>> '{hypothesis,id}',
    'evidence_ids',result.content #> '{proposal,evidence_ids}',
    'preconditions',result.content #> '{proposal,preconditions}',
    'unresolved_questions',result.content #> '{proposal,unresolved_questions}',
    'approval_eligible',FALSE,'execution_eligible',FALSE,
    'plan',jsonb_build_object('version',1,'status','draft',
      'summary','基于已记录候选生成的单步骤处置草案，需另行可信审批。',
      'risk_level','high','requires_human_approval',TRUE,'risk_policy_version','action-risk-v3',
      'steps',jsonb_build_array(result.content->'step')));
  IF plan.content IS DISTINCT FROM expected_content
    OR plan.content_sha256 IS DISTINCT FROM encode(sha256(convert_to(
      candidate_canonical_json(plan.content),'UTF8')),'hex') THEN
    RAISE EXCEPTION '候选草案不能改写原假设、证据、动作或审批执行资格';
  END IF;
  IF plan.created_at>=run.started_at+
     make_interval(secs=>(run.call_budget->>'max_duration_seconds')::int)
    OR EXISTS (SELECT 1 FROM investigation_call_reservations
               WHERE investigation_run_id=run.id AND status='reserved')
    OR (SELECT count(*) FROM investigation_call_reservations
        WHERE investigation_run_id=run.id AND kind='model')>
       (run.call_budget->>'max_model_calls')::int
    OR (SELECT count(*) FROM investigation_call_reservations
        WHERE investigation_run_id=run.id AND kind='tool')>
       (run.call_budget->>'max_tool_calls')::int
    OR EXISTS (SELECT 1 FROM investigation_call_reservations WHERE investigation_run_id=run.id
               AND kind='tool' AND task_id IS NOT NULL GROUP BY task_id
               HAVING count(*)>(run.call_budget->>'max_tool_calls_per_task')::int)
    OR EXISTS (SELECT 1 FROM investigation_call_reservations c
        WHERE c.investigation_run_id=run.id AND c.kind='model'
          AND (c.model_allocation IS NULL OR c.model_allocation='null'::jsonb
            OR (c.model_usage->>'input_tokens')::numeric>
               (c.model_allocation->>'estimated_input_tokens')::numeric
            OR (c.model_usage->>'output_tokens')::numeric>
               (c.model_allocation->>'output_token_limit')::numeric)) THEN
    RAISE EXCEPTION '候选草案不能延长原时限、遗留在途调用或跳过原累计额度';
  END IF;
  input_price:=(run.model_budget #>> '{profile,pricing,input_cost_per_million_usd}')::numeric;
  output_price:=(run.model_budget #>> '{profile,pricing,output_cost_per_million_usd}')::numeric;
  SELECT COALESCE(sum(greatest((model_allocation->>'estimated_input_tokens')::numeric,
                              COALESCE((model_usage->>'input_tokens')::numeric,0))),0),
         COALESCE(sum(greatest((model_allocation->>'output_token_limit')::numeric,
                              COALESCE((model_usage->>'output_tokens')::numeric,0))),0),
         COALESCE(sum(greatest((model_allocation->>'reserved_cost_usd')::numeric,
           ceil(COALESCE((model_usage->>'input_tokens')::numeric,0)*input_price+
                COALESCE((model_usage->>'output_tokens')::numeric,0)*output_price)/1000000)),0)
    INTO input_occupied,output_occupied,cost_occupied FROM investigation_call_reservations
    WHERE investigation_run_id=run.id AND kind='model';
  IF (input_occupied<=(run.model_budget->>'max_input_tokens')::numeric
    AND output_occupied<=(run.model_budget->>'max_output_tokens')::numeric
    AND cost_occupied<=(run.model_budget->>'max_estimated_cost_usd')::numeric) IS NOT TRUE THEN
    RAISE EXCEPTION '候选草案不能跳过原累计模型预算';
  END IF;
  SELECT * INTO admission FROM operator_authorizations WHERE id=plan.authorization_id;
  IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
    AND admission.role='engineer'
    AND admission.permissions ?& ARRAY['plan_action','manage_incident']
    AND admission.subject_id=plan.requested_by_id AND admission.method='POST'
    AND admission.operation='create_candidate_action_plan'
    AND admission.route=
      '/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan'
    AND admission.resources=jsonb_build_object('run_id',run.id::text,'job_id',job.id::text)
    AND admission.authorized_at<=plan.created_at) IS NOT TRUE THEN
    RAISE EXCEPTION '候选草案缺少同源可信工程师准入';
  END IF;
END;
$$ LANGUAGE plpgsql;

"""

_REQUESTS = """
CREATE FUNCTION assert_candidate_approval_request(request candidate_approval_requests)
RETURNS VOID AS $$
DECLARE
  plan candidate_action_plans%ROWTYPE;
  job investigation_candidate_evaluation_jobs%ROWTYPE;
  run investigation_runs%ROWTYPE;
  incident incidents%ROWTYPE;
  event incident_events%ROWTYPE;
  admission operator_authorizations%ROWTYPE;
  current_version INTEGER;
BEGIN
  SELECT * INTO admission FROM operator_authorizations WHERE id=request.authorization_id FOR UPDATE;
  PERFORM 1 FROM work_items WHERE investigation_run_id=request.investigation_run_id FOR UPDATE;
  SELECT * INTO job FROM investigation_candidate_evaluation_jobs
    WHERE id=request.candidate_job_id FOR UPDATE;
  SELECT * INTO run FROM investigation_runs WHERE id=request.investigation_run_id;
  SELECT * INTO incident FROM incidents WHERE id=run.incident_id FOR UPDATE;
  SELECT * INTO plan FROM candidate_action_plans WHERE id=request.plan_id;
  IF (plan.id IS NOT NULL AND plan.investigation_run_id=request.investigation_run_id
    AND plan.candidate_job_id=job.id AND job.investigation_run_id=run.id
    AND plan.content_sha256=request.plan_content_sha256
    AND plan.wait_version IS NOT DISTINCT FROM request.wait_version
    AND plan.incident_version=request.previous_incident_version
    AND request.incident_version=request.previous_incident_version+1
    AND plan.created_at<=request.created_at
    AND incident.status='awaiting_approval' AND incident.version=request.incident_version
    AND incident.updated_at=request.created_at) IS NOT TRUE THEN
    RAISE EXCEPTION '候选审批请求需要精确草案及迁移后的事故状态版本';
  END IF;
  -- 草案历史不使用 investigating/current 判断，避免本次合法迁移使自身来源失效。
  PERFORM assert_candidate_approval_plan_history(plan);
  IF NOT EXISTS (SELECT 1 FROM candidate_plan_commands audit
    WHERE audit.authorization_id=plan.authorization_id AND audit.plan_id=plan.id
      AND audit.outcome='applied' AND audit.command_sha256=plan.command_sha256
      AND audit.investigation_run_id=plan.investigation_run_id
      AND audit.candidate_job_id=plan.candidate_job_id AND audit.subject_id=plan.requested_by_id
      AND audit.created_at=plan.created_at) THEN
    RAISE EXCEPTION '候选审批请求缺少完整草案成功命令历史';
  END IF;
  SELECT max(state_version) INTO current_version FROM investigation_human_wait_events
    WHERE investigation_run_id=run.id;
  IF (job.source_kind='terminal_handoff' AND run.status='await_candidate') IS NOT TRUE
    AND (job.source_kind='human_route' AND run.status='await_human'
      AND current_version IS NOT DISTINCT FROM request.wait_version
      AND NOT EXISTS (SELECT 1 FROM investigation_human_wait_events
          WHERE investigation_run_id=run.id AND action='cancel')) IS NOT TRUE THEN
    RAISE EXCEPTION '候选审批请求的原输入已补充、取消或不再适用于审批阶段';
  END IF;
  IF (request.created_at<run.started_at+
        make_interval(secs=>(run.call_budget->>'max_duration_seconds')::int)
    AND request.expires_at=least(request.created_at+interval '30 minutes',run.started_at+
        make_interval(secs=>(run.call_budget->>'max_duration_seconds')::int))) IS NOT TRUE THEN
    RAISE EXCEPTION '候选审批请求不能续期或超出原运行截止时间';
  END IF;
  IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
    AND admission.role='engineer'
    AND admission.permissions ?& ARRAY['request_approval','manage_incident']
    AND admission.subject_id=request.requested_by_id AND admission.method='POST'
    AND admission.operation='create_candidate_approval_request'
    AND admission.route=
      '/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan/approval-request'
    AND admission.resources=jsonb_build_object('run_id',run.id::text,'job_id',job.id::text)
    AND admission.authorized_at<=request.created_at) IS NOT TRUE THEN
    RAISE EXCEPTION '候选审批请求缺少同源可信工程师准入';
  END IF;
  SELECT * INTO event FROM incident_events WHERE id=request.event_cursor;
  IF (event.incident_id=incident.id AND event.event_type='incident.status_changed'
    AND event.actor_type='human' AND event.actor_id=request.requested_by_id
    AND event.from_status='investigating' AND event.to_status='awaiting_approval'
    AND event.incident_version=request.incident_version AND event.occurred_at=request.created_at
    AND event.payload=jsonb_build_object('action','request_approval')) IS NOT TRUE THEN
    RAISE EXCEPTION '候选审批请求缺少精确事故审批迁移事件';
  END IF;
END;
$$ LANGUAGE plpgsql;

-- 事件声明不能代替真实的 OLD 状态；只约束候选草案对应版本的审批迁移。
CREATE FUNCTION guard_candidate_approval_incident() RETURNS trigger AS $$
BEGIN
  IF NEW.status='awaiting_approval'
    AND ROW(NEW.status,NEW.version) IS DISTINCT FROM ROW(OLD.status,OLD.version)
    AND EXISTS (SELECT 1 FROM candidate_action_plans p
      JOIN investigation_runs r ON r.id=p.investigation_run_id
      WHERE r.incident_id=NEW.id AND p.incident_version=NEW.version-1)
    AND (OLD.status='investigating' AND OLD.version=NEW.version-1) IS NOT TRUE THEN
    RAISE EXCEPTION '候选审批事故迁移必须从真实 investigating 状态及相邻版本开始';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_approval_incident_guard BEFORE UPDATE ON incidents
  FOR EACH ROW EXECUTE FUNCTION guard_candidate_approval_incident();

CREATE FUNCTION guard_candidate_approval_request() RETURNS trigger AS $$
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选审批请求不可更新或删除'; END IF;
  PERFORM assert_candidate_approval_request(NEW);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_approval_requests_guard BEFORE INSERT OR UPDATE OR DELETE
  ON candidate_approval_requests FOR EACH ROW EXECUTE FUNCTION guard_candidate_approval_request();

CREATE FUNCTION guard_candidate_approval_event() RETURNS trigger AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM candidate_approval_requests WHERE event_cursor=OLD.id) THEN
    RAISE EXCEPTION '候选审批请求绑定的事故事件不可更新或删除';
  END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_approval_event_guard BEFORE UPDATE OR DELETE ON incident_events
  FOR EACH ROW EXECUTE FUNCTION guard_candidate_approval_event();
"""

_STATE = """
CREATE FUNCTION validate_candidate_approval_state() RETURNS trigger AS $$
DECLARE
  request candidate_approval_requests%ROWTYPE;
  audit candidate_approval_commands%ROWTYPE;
  event incident_events%ROWTYPE;
  request_tx XID;
  event_tx XID;
  incident_tx XID;
  audit_tx XID;
BEGIN
  IF TG_TABLE_NAME='candidate_approval_requests' THEN
    SELECT * INTO request FROM candidate_approval_requests WHERE id=NEW.id;
  ELSIF NEW.outcome='applied' THEN
    SELECT * INTO request FROM candidate_approval_requests WHERE id=NEW.request_id;
  ELSE RETURN NULL;
  END IF;
  SELECT * INTO audit FROM candidate_approval_commands
    WHERE authorization_id=request.authorization_id;
  IF (request.id IS NOT NULL AND audit.authorization_id=request.authorization_id
    AND audit.outcome='applied' AND audit.request_id=request.id
    AND audit.created_at=request.created_at
    AND audit.investigation_run_id=request.investigation_run_id
    AND audit.candidate_job_id=request.candidate_job_id AND audit.subject_id=request.requested_by_id
    AND audit.command_sha256=request.command_sha256) IS NOT TRUE THEN
    RAISE EXCEPTION '候选审批请求缺少同事务精确成功命令闭环';
  END IF;
  SELECT * INTO event FROM incident_events WHERE id=request.event_cursor;
  SELECT xmin INTO request_tx FROM candidate_approval_requests WHERE id=request.id;
  SELECT xmin INTO event_tx FROM incident_events WHERE id=request.event_cursor;
  SELECT xmin INTO audit_tx FROM candidate_approval_commands WHERE cursor=audit.cursor;
  SELECT i.xmin INTO incident_tx FROM incidents i JOIN investigation_runs r ON r.incident_id=i.id
    WHERE r.id=request.investigation_run_id;
  IF event.reason IS DISTINCT FROM audit.command->>'reason'
    OR request_tx IS DISTINCT FROM event_tx OR request_tx IS DISTINCT FROM audit_tx
    OR request_tx IS DISTINCT FROM incident_tx THEN
    RAISE EXCEPTION '候选审批请求、事故迁移事件和命令必须同事务精确绑定';
  END IF;
  PERFORM assert_candidate_approval_request(request);
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER candidate_approval_requests_state
  AFTER INSERT ON candidate_approval_requests
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_candidate_approval_state();
CREATE CONSTRAINT TRIGGER candidate_approval_commands_state
  AFTER INSERT ON candidate_approval_commands
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_candidate_approval_state();
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM candidate_approval_requests)
          OR EXISTS (SELECT 1 FROM candidate_approval_commands) THEN
          RAISE EXCEPTION '不能删除已保存的候选审批事实或命令审计';
        END IF;
      END $$;
      DROP TRIGGER candidate_approval_requests_state ON candidate_approval_requests;
      DROP TRIGGER candidate_approval_commands_state ON candidate_approval_commands;
      DROP FUNCTION validate_candidate_approval_state();
      DROP TRIGGER candidate_approval_incident_guard ON incidents;
      DROP FUNCTION guard_candidate_approval_incident();
      DROP TRIGGER candidate_approval_event_guard ON incident_events;
      DROP FUNCTION guard_candidate_approval_event();
      DROP TRIGGER candidate_approval_requests_guard ON candidate_approval_requests;
      DROP FUNCTION guard_candidate_approval_request();
      DROP FUNCTION assert_candidate_approval_request(candidate_approval_requests);
      DROP FUNCTION assert_candidate_approval_plan_history(candidate_action_plans);
      DROP TRIGGER candidate_approval_commands_guard ON candidate_approval_commands;
      DROP FUNCTION guard_candidate_approval_command();
      DROP FUNCTION normalize_candidate_approval_command(TEXT);
      DROP TABLE candidate_approval_commands;
      DROP TABLE candidate_approval_requests;
    """)
