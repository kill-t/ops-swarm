"""候选审批的可信决定、原始命令与事故迁移不可变闭环。"""

from alembic import op

revision = "0075_candidate_decision"
down_revision = "0074_candidate_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(_COMMANDS)
    op.execute(_HISTORY)
    op.execute(_DECISIONS)
    op.execute(_STATE)


_HISTORY = """

CREATE FUNCTION assert_candidate_decision_plan_history(
  plan candidate_action_plans, require_budget BOOLEAN)
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
  -- 负面决定可放宽累计容量，不能把原成功调用的损坏用量当成完整历史。
  IF source_call.model_usage IS NOT NULL AND (
    jsonb_typeof(source_call.model_usage)='object'
    AND source_call.model_usage ?& ARRAY['input_tokens','output_tokens','total_tokens']
    AND (source_call.model_usage-ARRAY['input_tokens','output_tokens','total_tokens'])='{}'::jsonb
    AND jsonb_typeof(source_call.model_usage->'input_tokens')='number'
    AND jsonb_typeof(source_call.model_usage->'output_tokens')='number'
    AND jsonb_typeof(source_call.model_usage->'total_tokens')='number'
    AND source_call.model_usage->>'input_tokens' ~ '^[0-9]+$'
    AND source_call.model_usage->>'output_tokens' ~ '^[0-9]+$'
    AND source_call.model_usage->>'total_tokens' ~ '^[0-9]+$'
    AND (source_call.model_usage->>'input_tokens')::numeric<=1000000000
    AND (source_call.model_usage->>'output_tokens')::numeric<=1000000000
    AND (source_call.model_usage->>'total_tokens')::numeric<=2000000000
    AND (source_call.model_usage->>'total_tokens')::numeric=
      (source_call.model_usage->>'input_tokens')::numeric+
      (source_call.model_usage->>'output_tokens')::numeric
    AND (source_call.model_usage->>'input_tokens')::numeric<=
      (source_call.model_allocation->>'estimated_input_tokens')::numeric
    AND (source_call.model_usage->>'output_tokens')::numeric<=
      (source_call.model_allocation->>'output_token_limit')::numeric
  ) IS NOT TRUE THEN
    RAISE EXCEPTION '候选成功调用用量缺少完整记录或超过原分配';
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
  IF require_budget THEN
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


CREATE FUNCTION assert_candidate_decision_request_history(
  request candidate_approval_requests, require_budget BOOLEAN)
RETURNS VOID AS $$
DECLARE
  plan candidate_action_plans%ROWTYPE;
  job investigation_candidate_evaluation_jobs%ROWTYPE;
  run investigation_runs%ROWTYPE;
  incident incidents%ROWTYPE;
  event incident_events%ROWTYPE;
  admission operator_authorizations%ROWTYPE;
BEGIN
  SELECT * INTO admission FROM operator_authorizations WHERE id=request.authorization_id;
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
) IS NOT TRUE THEN
    RAISE EXCEPTION '候选审批请求需要精确草案及迁移后的事故状态版本';
  END IF;
  -- 草案历史不使用 investigating/current 判断，避免本次合法迁移使自身来源失效。
  PERFORM assert_candidate_decision_plan_history(plan,require_budget);
  IF NOT EXISTS (SELECT 1 FROM candidate_plan_commands audit
    WHERE audit.authorization_id=plan.authorization_id AND audit.plan_id=plan.id
      AND audit.outcome='applied' AND audit.command_sha256=plan.command_sha256
      AND audit.investigation_run_id=plan.investigation_run_id
      AND audit.candidate_job_id=plan.candidate_job_id AND audit.subject_id=plan.requested_by_id
      AND audit.created_at=plan.created_at) THEN
    RAISE EXCEPTION '候选审批请求缺少完整草案成功命令历史';
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
  IF NOT EXISTS (SELECT 1 FROM candidate_approval_commands audit
    WHERE audit.authorization_id=request.authorization_id AND audit.request_id=request.id
      AND audit.outcome='applied' AND audit.created_at=request.created_at
      AND audit.investigation_run_id=request.investigation_run_id
      AND audit.candidate_job_id=request.candidate_job_id
      AND audit.subject_id=request.requested_by_id
      AND audit.command_sha256=request.command_sha256
      AND normalize_candidate_approval_command(audit.raw_body)=audit.command
      AND audit.command_sha256=encode(sha256(convert_to(
        candidate_canonical_json(audit.command),'UTF8')),'hex')
      AND admission.body_sha256=encode(sha256(convert_to(audit.raw_body,'UTF8')),'hex')
      AND audit.command->>'plan_content_sha256'=request.plan_content_sha256
      AND audit.command->'expected_incident_version'=to_jsonb(request.previous_incident_version)
      AND audit.command->'expected_wait_version'=
          COALESCE(to_jsonb(request.wait_version),'null'::jsonb)
      AND audit.command->>'idempotency_key'=request.idempotency_key) THEN
    RAISE EXCEPTION '候选决定缺少原请求精确成功审计历史';
  END IF;
  SELECT * INTO event FROM incident_events WHERE id=request.event_cursor;
  IF (event.incident_id=incident.id AND event.event_type='incident.status_changed'
    AND event.actor_type='human' AND event.actor_id=request.requested_by_id
    AND event.from_status='investigating' AND event.to_status='awaiting_approval'
    AND event.incident_version=request.incident_version AND event.occurred_at=request.created_at
    AND event.payload=jsonb_build_object('action','request_approval')
    AND event.reason=(SELECT command->>'reason' FROM candidate_approval_commands
      WHERE authorization_id=request.authorization_id)) IS NOT TRUE THEN
    RAISE EXCEPTION '候选审批请求缺少精确事故审批迁移事件';
  END IF;
END;
$$ LANGUAGE plpgsql;

"""


_TABLES = """
CREATE TABLE candidate_approval_decisions (
  id UUID NOT NULL PRIMARY KEY,
  investigation_run_id UUID NOT NULL,
  candidate_job_id UUID NOT NULL,
  request_id UUID NOT NULL REFERENCES candidate_approval_requests(id) ON DELETE RESTRICT,
  plan_content_sha256 VARCHAR(64) NOT NULL,
  previous_incident_version INTEGER NOT NULL,
  incident_version INTEGER NOT NULL,
  event_cursor BIGINT NOT NULL REFERENCES incident_events(id) ON DELETE RESTRICT,
  outcome VARCHAR(16) NOT NULL,
  policy_version VARCHAR(40) NOT NULL,
  decided_by_id VARCHAR(100) NOT NULL,
  authorization_id UUID NOT NULL REFERENCES operator_authorizations(id) ON DELETE RESTRICT,
  idempotency_key VARCHAR(100) NOT NULL,
  command_sha256 VARCHAR(64) NOT NULL,
  content JSONB NOT NULL,
  content_sha256 VARCHAR(64) NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  CONSTRAINT fk_candidate_decisions_job_run FOREIGN KEY(candidate_job_id,investigation_run_id)
    REFERENCES investigation_candidate_evaluation_jobs(id,investigation_run_id) ON DELETE RESTRICT,
  CONSTRAINT uq_candidate_decisions_request UNIQUE(request_id),
  CONSTRAINT uq_candidate_decisions_authorization UNIQUE(authorization_id),
  CONSTRAINT uq_candidate_decisions_event UNIQUE(event_cursor),
  CONSTRAINT uq_candidate_decisions_key UNIQUE(investigation_run_id,decided_by_id,idempotency_key),
  CONSTRAINT ck_candidate_decisions_policy CHECK (policy_version='candidate-approval-decision-v1'),
  CONSTRAINT ck_candidate_decisions_outcome CHECK (outcome IN ('approved','rejected','expired')),
  CONSTRAINT ck_candidate_decisions_versions CHECK
    (previous_incident_version>=1 AND incident_version=previous_incident_version+1),
  CONSTRAINT ck_candidate_decisions_hashes CHECK (plan_content_sha256 ~ '^[0-9a-f]{64}$'
    AND command_sha256 ~ '^[0-9a-f]{64}$' AND content_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_candidate_decisions_content CHECK
    (jsonb_typeof(content)='object' AND octet_length(content::text)<=65536),
  CONSTRAINT ck_candidate_decisions_subject CHECK
    (char_length(btrim(decided_by_id)) BETWEEN 1 AND 100),
  CONSTRAINT ck_candidate_decisions_key CHECK (char_length(idempotency_key) BETWEEN 8 AND 100
    AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$')
);
CREATE TABLE candidate_decision_commands (
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
  decision_id UUID REFERENCES candidate_approval_decisions(id) ON DELETE RESTRICT,
  error_code VARCHAR(100),
  error_message VARCHAR(1000),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  CONSTRAINT uq_candidate_decision_commands_authorization UNIQUE(authorization_id),
  CONSTRAINT ck_candidate_decision_commands_body CHECK
    (jsonb_typeof(command)='object' AND octet_length(command::text)<=32768),
  CONSTRAINT ck_candidate_decision_commands_raw CHECK
    (octet_length(raw_body) BETWEEN 2 AND 65536),
  CONSTRAINT ck_candidate_decision_commands_hash CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_candidate_decision_commands_subject CHECK
    (char_length(btrim(subject_id)) BETWEEN 1 AND 100),
  CONSTRAINT ck_candidate_decision_commands_outcome CHECK
    ((outcome IN ('applied','replayed') AND decision_id IS NOT NULL AND error_code IS NULL
      AND error_message IS NULL AND ((outcome='applied' AND http_status=201)
      OR (outcome='replayed' AND http_status=200)))
    OR (outcome='error' AND decision_id IS NULL AND error_code IS NOT NULL
      AND error_message IS NOT NULL AND http_status IN (404,409))),
  CONSTRAINT ck_candidate_decision_commands_error CHECK
    (error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,100}$')
);
CREATE INDEX ix_candidate_decision_commands_run_cursor
  ON candidate_decision_commands(investigation_run_id,cursor);
"""

_COMMANDS = """
CREATE FUNCTION normalize_candidate_decision_command(raw_body TEXT) RETURNS JSONB AS $$
DECLARE body JSONB; request_uuid UUID; request_text TEXT; uuid_pattern TEXT;
BEGIN
  body:=raw_body::jsonb;
  IF (jsonb_typeof(body)='object'
    AND body ?& ARRAY['action','request_id','plan_content_sha256',
                     'expected_incident_version','idempotency_key','reason']
    AND (body-ARRAY['action','request_id','plan_content_sha256',
                   'expected_incident_version','acknowledge_unresolved_conditions',
                   'idempotency_key','reason'])='{}'::jsonb
    AND jsonb_typeof(body->'action')='string' AND body->>'action' IN ('approve','reject','expire')
    AND jsonb_typeof(body->'request_id')='string'
    AND jsonb_typeof(body->'plan_content_sha256')='string'
    AND jsonb_typeof(body->'idempotency_key')='string'
    AND jsonb_typeof(body->'reason')='string'
    AND (NOT body ? 'acknowledge_unresolved_conditions'
      OR jsonb_typeof(body->'acknowledge_unresolved_conditions')='boolean')
    AND ((body->>'action'='approve' AND body->'acknowledge_unresolved_conditions'='true'::jsonb)
      OR (body->>'action' IN ('reject','expire')
          AND COALESCE(body->'acknowledge_unresolved_conditions','false'::jsonb)='false'::jsonb))
    AND jsonb_typeof(body->'expected_incident_version')='number'
    AND raw_body::json->>'expected_incident_version' ~ '^[0-9]+$'
    AND (body->>'expected_incident_version')::numeric>=1) IS NOT TRUE THEN
    RETURN NULL;
  END IF;
  request_text:=body->>'request_id';
  uuid_pattern:='[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-' ||
                '[0-9a-fA-F]{4}-[0-9a-fA-F]{12}';
  -- UUID 字段不使用字符串裁剪；只接受 Pydantic 允许的四类文本形式。
  IF request_text ~ ('^urn:uuid:'||uuid_pattern||'$') THEN
    request_text:=substr(request_text,10);
  ELSIF request_text !~ ('^('||uuid_pattern||'|[0-9a-fA-F]{32}|[{]'||
                         uuid_pattern||'[}])$') THEN
    RETURN NULL;
  END IF;
  request_uuid:=request_text::uuid;
  RETURN jsonb_build_object('action',body->>'action','request_id',request_uuid::text,
    'plan_content_sha256',candidate_strip_text(body->>'plan_content_sha256'),
    'expected_incident_version',body->'expected_incident_version',
    'acknowledge_unresolved_conditions',
      COALESCE(body->'acknowledge_unresolved_conditions','false'::jsonb),
    'idempotency_key',candidate_strip_text(body->>'idempotency_key'),
    'reason',candidate_strip_text(body->>'reason'));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
  RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

CREATE FUNCTION guard_candidate_decision_command() RETURNS trigger AS $$
DECLARE admission operator_authorizations%ROWTYPE; decision candidate_approval_decisions%ROWTYPE;
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选决定命令审计不可更新或删除'; END IF;
  SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
  IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
    AND admission.role='approver' AND admission.permissions ? 'decide_approval'
    AND admission.subject_id=NEW.subject_id AND admission.method='POST'
    AND admission.operation='decide_candidate_approval'
    AND admission.route=
      '/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan/approval-request/decision'
    AND admission.resources=jsonb_build_object('run_id',NEW.investigation_run_id::text,
                                              'job_id',NEW.candidate_job_id::text)
    AND admission.authorized_at<=NEW.created_at
    AND admission.body_sha256=encode(sha256(convert_to(NEW.raw_body,'UTF8')),'hex'))
    IS NOT TRUE THEN RAISE EXCEPTION '候选决定命令缺少精确原始正文或同源可信准入'; END IF;
  IF (NEW.command->>'plan_content_sha256' ~ '^[0-9a-f]{64}$'
    AND char_length(NEW.command->>'idempotency_key') BETWEEN 8 AND 100
    AND NEW.command->>'idempotency_key' ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'
    AND char_length(candidate_strip_text(NEW.command->>'reason')) BETWEEN 1 AND 500
    AND NEW.command_sha256=encode(sha256(convert_to(
      candidate_canonical_json(NEW.command),'UTF8')),'hex')) IS NOT TRUE THEN
    RAISE EXCEPTION '候选决定命令缺少有界精确规范来源绑定';
  END IF;
  IF normalize_candidate_decision_command(NEW.raw_body) IS DISTINCT FROM NEW.command THEN
    RAISE EXCEPTION '候选决定命令原始正文与规范命令不一致';
  END IF;
  IF NEW.outcome IN ('applied','replayed') THEN
    SELECT * INTO decision FROM candidate_approval_decisions WHERE id=NEW.decision_id;
    IF (decision.id IS NOT NULL AND decision.investigation_run_id=NEW.investigation_run_id
      AND decision.candidate_job_id=NEW.candidate_job_id AND decision.decided_by_id=NEW.subject_id
      AND decision.command_sha256=NEW.command_sha256
      AND decision.request_id::text=NEW.command->>'request_id'
      AND decision.plan_content_sha256=NEW.command->>'plan_content_sha256'
      AND decision.outcome=CASE NEW.command->>'action'
        WHEN 'approve' THEN 'approved' WHEN 'reject' THEN 'rejected' ELSE 'expired' END
      AND to_jsonb(decision.previous_incident_version)=NEW.command->'expected_incident_version'
      AND decision.idempotency_key=NEW.command->>'idempotency_key'
      AND decision.created_at<=NEW.created_at
      AND ((NEW.outcome='applied' AND decision.authorization_id=NEW.authorization_id
        AND decision.created_at=NEW.created_at)
        OR (NEW.outcome='replayed' AND decision.authorization_id<>NEW.authorization_id)))
      IS NOT TRUE THEN RAISE EXCEPTION '候选决定命令结果与原始决定不一致'; END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_decision_commands_guard BEFORE INSERT OR UPDATE OR DELETE
  ON candidate_decision_commands FOR EACH ROW EXECUTE FUNCTION guard_candidate_decision_command();
"""


_DECISIONS = """
CREATE FUNCTION assert_candidate_approval_decision(decision candidate_approval_decisions)
RETURNS VOID AS $$
DECLARE
  request candidate_approval_requests%ROWTYPE;
  plan candidate_action_plans%ROWTYPE;
  job investigation_candidate_evaluation_jobs%ROWTYPE;
  run investigation_runs%ROWTYPE;
  incident incidents%ROWTYPE;
  event incident_events%ROWTYPE;
  admission operator_authorizations%ROWTYPE;
  current_version INTEGER;
  expected_content JSONB;
BEGIN
  SELECT * INTO admission FROM operator_authorizations
    WHERE id=decision.authorization_id FOR UPDATE;
  PERFORM 1 FROM work_items WHERE investigation_run_id=decision.investigation_run_id FOR UPDATE;
  SELECT * INTO job FROM investigation_candidate_evaluation_jobs
    WHERE id=decision.candidate_job_id FOR UPDATE;
  SELECT * INTO run FROM investigation_runs WHERE id=decision.investigation_run_id;
  SELECT * INTO incident FROM incidents WHERE id=run.incident_id FOR UPDATE;
  SELECT * INTO request FROM candidate_approval_requests WHERE id=decision.request_id;
  SELECT * INTO plan FROM candidate_action_plans WHERE id=request.plan_id;
  IF (request.id IS NOT NULL AND request.investigation_run_id=run.id
    AND request.candidate_job_id=job.id AND job.investigation_run_id=run.id
    AND decision.plan_content_sha256=request.plan_content_sha256
    AND decision.previous_incident_version=request.incident_version
    AND decision.incident_version=request.incident_version+1
    AND request.created_at<=decision.created_at
    AND incident.version=decision.incident_version AND incident.updated_at=decision.created_at
    AND incident.status=CASE decision.outcome WHEN 'approved' THEN 'mitigating'
      ELSE 'investigating' END) IS NOT TRUE THEN
    RAISE EXCEPTION '候选决定需要精确请求及迁移后的事故状态版本';
  END IF;
  -- 当前阶段已推进；历史重建只校验真实来源，不能要求原 pending 的事故状态。
  -- 负面决定不消费模型资源，不能因累计预算耗尽阻止拒绝或过期收敛。
  PERFORM assert_candidate_decision_request_history(request,decision.outcome='approved');
  IF decision.outcome='approved' THEN
    SELECT max(state_version) INTO current_version FROM investigation_human_wait_events
      WHERE investigation_run_id=run.id;
    IF (job.source_kind='terminal_handoff' AND run.status='await_candidate') IS NOT TRUE
      AND (job.source_kind='human_route' AND run.status='await_human'
        AND current_version IS NOT DISTINCT FROM request.wait_version
        AND NOT EXISTS (SELECT 1 FROM investigation_human_wait_events
          WHERE investigation_run_id=run.id AND action='cancel')) IS NOT TRUE THEN
      RAISE EXCEPTION '候选批准的原输入已补充、取消或不再适用';
    END IF;
  END IF;
  IF (decision.outcome='expired' AND decision.created_at>=request.expires_at) IS NOT TRUE
    AND (decision.outcome IN ('approved','rejected')
      AND decision.created_at<request.expires_at) IS NOT TRUE THEN
    RAISE EXCEPTION '候选决定动作必须匹配原请求截止时间';
  END IF;
  IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
    AND admission.role='approver' AND admission.permissions ? 'decide_approval'
    AND admission.subject_id=decision.decided_by_id AND admission.method='POST'
    AND admission.operation='decide_candidate_approval'
    AND admission.route=
      '/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan/approval-request/decision'
    AND admission.resources=jsonb_build_object('run_id',run.id::text,'job_id',job.id::text)
    AND admission.authorized_at<=decision.created_at) IS NOT TRUE THEN
    RAISE EXCEPTION '候选决定缺少同源可信审批人准入';
  END IF;
  expected_content:=jsonb_build_object('version','candidate-approval-decision-content-v1',
    'request_id',request.id::text,'plan_id',plan.id::text,
    'plan_content_sha256',plan.content_sha256,'candidate_job_id',job.id::text,
    'source_content_sha256',job.source_content_sha256,'wait_version',request.wait_version,
    'outcome',decision.outcome,'evidence_ids',plan.content->'evidence_ids',
    'preconditions',plan.content->'preconditions',
    'unresolved_questions',plan.content->'unresolved_questions','execution_eligible',FALSE);
  IF decision.content IS DISTINCT FROM expected_content
    OR decision.content_sha256 IS DISTINCT FROM encode(sha256(convert_to(
      candidate_canonical_json(decision.content),'UTF8')),'hex') THEN
    RAISE EXCEPTION '候选决定不能改写原证据、约束、未解问题或执行资格';
  END IF;
  SELECT * INTO event FROM incident_events WHERE id=decision.event_cursor;
  IF (event.incident_id=incident.id AND event.event_type='incident.status_changed'
    AND event.actor_type='human' AND event.actor_id=decision.decided_by_id
    AND event.from_status='awaiting_approval' AND event.to_status=incident.status
    AND event.incident_version=decision.incident_version AND event.occurred_at=decision.created_at
    AND event.payload=jsonb_build_object('action',CASE decision.outcome
      WHEN 'approved' THEN 'approve_plan' WHEN 'rejected' THEN 'reject_plan'
      ELSE 'expire_approval' END)) IS NOT TRUE THEN
    RAISE EXCEPTION '候选决定缺少精确事故迁移事件';
  END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION guard_candidate_approval_decision() RETURNS trigger AS $$
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选审批决定不可更新或删除'; END IF;
  PERFORM assert_candidate_approval_decision(NEW);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_approval_decisions_guard BEFORE INSERT OR UPDATE OR DELETE
  ON candidate_approval_decisions FOR EACH ROW EXECUTE FUNCTION guard_candidate_approval_decision();

CREATE FUNCTION guard_candidate_decision_incident() RETURNS trigger AS $$
BEGIN
  IF NEW.status IN ('mitigating','investigating')
    AND ROW(NEW.status,NEW.version) IS DISTINCT FROM ROW(OLD.status,OLD.version)
    AND EXISTS (SELECT 1 FROM candidate_approval_requests a
      JOIN investigation_runs r ON r.id=a.investigation_run_id
      WHERE r.incident_id=NEW.id AND a.incident_version=NEW.version-1)
    AND (OLD.status='awaiting_approval' AND OLD.version=NEW.version-1) IS NOT TRUE THEN
    RAISE EXCEPTION '候选决定事故迁移必须从真实 awaiting_approval 状态及相邻版本开始';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_decision_incident_guard BEFORE UPDATE ON incidents
  FOR EACH ROW EXECUTE FUNCTION guard_candidate_decision_incident();

CREATE FUNCTION guard_candidate_decision_event() RETURNS trigger AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM candidate_approval_decisions WHERE event_cursor=OLD.id) THEN
    RAISE EXCEPTION '候选审批决定绑定的事故事件不可更新或删除';
  END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_decision_event_guard BEFORE UPDATE OR DELETE ON incident_events
  FOR EACH ROW EXECUTE FUNCTION guard_candidate_decision_event();
"""

_STATE = """
CREATE FUNCTION validate_candidate_decision_state() RETURNS trigger AS $$
DECLARE
  decision candidate_approval_decisions%ROWTYPE;
  audit candidate_decision_commands%ROWTYPE;
  event incident_events%ROWTYPE;
  decision_tx XID;
  event_tx XID;
  incident_tx XID;
  audit_tx XID;
BEGIN
  IF TG_TABLE_NAME='candidate_approval_decisions' THEN
    SELECT * INTO decision FROM candidate_approval_decisions WHERE id=NEW.id;
  ELSIF NEW.outcome='applied' THEN
    SELECT * INTO decision FROM candidate_approval_decisions WHERE id=NEW.decision_id;
  ELSE RETURN NULL;
  END IF;
  SELECT * INTO audit FROM candidate_decision_commands
    WHERE authorization_id=decision.authorization_id;
  IF (decision.id IS NOT NULL AND audit.authorization_id=decision.authorization_id
    AND audit.outcome='applied' AND audit.decision_id=decision.id
    AND audit.created_at=decision.created_at
    AND audit.investigation_run_id=decision.investigation_run_id
    AND audit.candidate_job_id=decision.candidate_job_id AND audit.subject_id=decision.decided_by_id
    AND audit.command_sha256=decision.command_sha256) IS NOT TRUE THEN
    RAISE EXCEPTION '候选决定缺少同事务精确成功命令闭环';
  END IF;
  SELECT * INTO event FROM incident_events WHERE id=decision.event_cursor;
  SELECT xmin INTO decision_tx FROM candidate_approval_decisions WHERE id=decision.id;
  SELECT xmin INTO event_tx FROM incident_events WHERE id=decision.event_cursor;
  SELECT xmin INTO audit_tx FROM candidate_decision_commands WHERE cursor=audit.cursor;
  SELECT i.xmin INTO incident_tx FROM incidents i JOIN investigation_runs r ON r.incident_id=i.id
    WHERE r.id=decision.investigation_run_id;
  IF event.reason IS DISTINCT FROM audit.command->>'reason'
    OR decision_tx IS DISTINCT FROM event_tx OR decision_tx IS DISTINCT FROM audit_tx
    OR decision_tx IS DISTINCT FROM incident_tx THEN
    RAISE EXCEPTION '候选决定、事故迁移事件和命令必须同事务精确绑定';
  END IF;
  PERFORM assert_candidate_approval_decision(decision);
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER candidate_approval_decisions_state
  AFTER INSERT ON candidate_approval_decisions
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_candidate_decision_state();
CREATE CONSTRAINT TRIGGER candidate_decision_commands_state
  AFTER INSERT ON candidate_decision_commands
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_candidate_decision_state();
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM candidate_approval_decisions)
          OR EXISTS (SELECT 1 FROM candidate_decision_commands) THEN
          RAISE EXCEPTION '不能删除已保存的候选决定事实或命令审计';
        END IF;
      END $$;
      DROP TRIGGER candidate_approval_decisions_state ON candidate_approval_decisions;
      DROP TRIGGER candidate_decision_commands_state ON candidate_decision_commands;
      DROP FUNCTION validate_candidate_decision_state();
      DROP TRIGGER candidate_decision_incident_guard ON incidents;
      DROP FUNCTION guard_candidate_decision_incident();
      DROP TRIGGER candidate_decision_event_guard ON incident_events;
      DROP FUNCTION guard_candidate_decision_event();
      DROP TRIGGER candidate_approval_decisions_guard ON candidate_approval_decisions;
      DROP FUNCTION guard_candidate_approval_decision();
      DROP FUNCTION assert_candidate_approval_decision(candidate_approval_decisions);
      DROP FUNCTION assert_candidate_decision_request_history(candidate_approval_requests,BOOLEAN);
      DROP FUNCTION assert_candidate_decision_plan_history(candidate_action_plans,BOOLEAN);
      DROP TRIGGER candidate_decision_commands_guard ON candidate_decision_commands;
      DROP FUNCTION guard_candidate_decision_command();
      DROP FUNCTION normalize_candidate_decision_command(TEXT);
      DROP TABLE candidate_decision_commands;
      DROP TABLE candidate_approval_decisions;
    """)
