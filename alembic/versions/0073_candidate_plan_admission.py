"""独立候选草案、可信命令与不授予审批执行资格的原生准入。"""

from alembic import op

revision = "0073_candidate_plan_admission"
down_revision = "0072_candidate_evaluation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(_COMMANDS)
    op.execute(_PLANS)
    op.execute(_STATE)


_TABLES = """
CREATE TABLE candidate_action_plans (
  id UUID NOT NULL PRIMARY KEY,
  investigation_run_id UUID NOT NULL,
  candidate_job_id UUID NOT NULL,
  candidate_result_sha256 VARCHAR(64) NOT NULL,
  wait_version INTEGER,
  incident_version INTEGER NOT NULL,
  policy_version VARCHAR(40) NOT NULL,
  requested_by_id VARCHAR(100) NOT NULL,
  authorization_id UUID NOT NULL REFERENCES operator_authorizations(id) ON DELETE RESTRICT,
  idempotency_key VARCHAR(100) NOT NULL,
  command_sha256 VARCHAR(64) NOT NULL,
  content JSONB NOT NULL,
  content_sha256 VARCHAR(64) NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  CONSTRAINT fk_candidate_plans_job_run FOREIGN KEY(candidate_job_id,investigation_run_id)
    REFERENCES investigation_candidate_evaluation_jobs(id,investigation_run_id) ON DELETE RESTRICT,
  CONSTRAINT uq_candidate_plans_job UNIQUE(candidate_job_id),
  CONSTRAINT uq_candidate_plans_id_run UNIQUE(id,investigation_run_id),
  CONSTRAINT uq_candidate_plans_authorization UNIQUE(authorization_id),
  CONSTRAINT uq_candidate_plans_key UNIQUE(investigation_run_id,requested_by_id,idempotency_key),
  CONSTRAINT ck_candidate_plans_policy CHECK (policy_version='candidate-plan-v1'),
  CONSTRAINT ck_candidate_plans_wait_version CHECK
    (wait_version IS NULL OR wait_version BETWEEN 2 AND 16),
  CONSTRAINT ck_candidate_plans_incident_version CHECK (incident_version>=1),
  CONSTRAINT ck_candidate_plans_hashes CHECK (candidate_result_sha256 ~ '^[0-9a-f]{64}$'
    AND command_sha256 ~ '^[0-9a-f]{64}$' AND content_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_candidate_plans_content CHECK
    (jsonb_typeof(content)='object' AND octet_length(content::text)<=65536),
  CONSTRAINT ck_candidate_plans_subject CHECK
    (char_length(btrim(requested_by_id)) BETWEEN 1 AND 100),
  CONSTRAINT ck_candidate_plans_key CHECK (char_length(idempotency_key) BETWEEN 8 AND 100
    AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$')
);
CREATE TABLE candidate_plan_commands (
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
  plan_id UUID REFERENCES candidate_action_plans(id) ON DELETE RESTRICT,
  error_code VARCHAR(100),
  error_message VARCHAR(1000),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  CONSTRAINT uq_candidate_plan_commands_authorization UNIQUE(authorization_id),
  CONSTRAINT ck_candidate_plan_commands_body CHECK
    (jsonb_typeof(command)='object' AND octet_length(command::text)<=32768),
  CONSTRAINT ck_candidate_plan_commands_raw CHECK (octet_length(raw_body) BETWEEN 2 AND 65536),
  CONSTRAINT ck_candidate_plan_commands_hash CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_candidate_plan_commands_subject CHECK
    (char_length(btrim(subject_id)) BETWEEN 1 AND 100),
  CONSTRAINT ck_candidate_plan_commands_outcome CHECK
    ((outcome IN ('applied','replayed') AND plan_id IS NOT NULL AND error_code IS NULL
      AND error_message IS NULL AND ((outcome='applied' AND http_status=201)
      OR (outcome='replayed' AND http_status=200)))
    OR (outcome='error' AND plan_id IS NULL AND error_code IS NOT NULL
      AND error_message IS NOT NULL AND http_status IN (404,409))),
  CONSTRAINT ck_candidate_plan_commands_error CHECK
    (error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,100}$')
);
CREATE INDEX ix_candidate_plan_commands_run_cursor
  ON candidate_plan_commands(investigation_run_id,cursor);
"""

_COMMANDS = """
CREATE FUNCTION normalize_candidate_plan_command(raw_body TEXT) RETURNS JSONB AS $$
DECLARE body JSONB;
BEGIN
  body:=raw_body::jsonb;
  IF (jsonb_typeof(body)='object'
    AND body ?& ARRAY['candidate_result_sha256','expected_wait_version',
                     'expected_incident_version','idempotency_key','reason']
    AND (body-ARRAY['action','candidate_result_sha256','expected_wait_version',
                   'expected_incident_version','idempotency_key','reason'])='{}'::jsonb
    AND (NOT body ? 'action' OR (jsonb_typeof(body->'action')='string'
                                AND body->>'action'='create_plan'))
    AND jsonb_typeof(body->'candidate_result_sha256')='string'
    AND jsonb_typeof(body->'idempotency_key')='string'
    AND jsonb_typeof(body->'reason')='string'
    AND (body->'expected_wait_version'='null'::jsonb OR
      (jsonb_typeof(body->'expected_wait_version')='number'
        AND raw_body::json->>'expected_wait_version' ~ '^[0-9]+$'
        AND body->'expected_wait_version' IN
          (SELECT to_jsonb(value) FROM generate_series(2,16) value)))
    AND jsonb_typeof(body->'expected_incident_version')='number'
    AND raw_body::json->>'expected_incident_version' ~ '^[0-9]+$'
    AND (body->>'expected_incident_version')::numeric>=1) IS NOT TRUE THEN
    RETURN NULL;
  END IF;
  RETURN jsonb_build_object('action','create_plan',
    'candidate_result_sha256',candidate_strip_text(body->>'candidate_result_sha256'),
    'expected_wait_version',body->'expected_wait_version',
    'expected_incident_version',body->'expected_incident_version',
    'idempotency_key',candidate_strip_text(body->>'idempotency_key'),
    'reason',candidate_strip_text(body->>'reason'));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
  RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

CREATE FUNCTION guard_candidate_plan_command() RETURNS trigger AS $$
DECLARE
  admission operator_authorizations%ROWTYPE;
  plan candidate_action_plans%ROWTYPE;
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选规划命令审计不可更新或删除'; END IF;
  SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
  IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
    AND admission.role='engineer'
    AND admission.permissions ?& ARRAY['plan_action','manage_incident']
    AND admission.subject_id=NEW.subject_id AND admission.method='POST'
    AND admission.operation='create_candidate_action_plan'
    AND admission.route=
      '/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan'
    AND admission.resources=jsonb_build_object('run_id',NEW.investigation_run_id::text,
                                              'job_id',NEW.candidate_job_id::text)
    AND admission.authorized_at<=NEW.created_at
    AND admission.body_sha256=encode(sha256(convert_to(NEW.raw_body,'UTF8')),'hex'))
    IS NOT TRUE THEN
    RAISE EXCEPTION '候选规划命令缺少精确原始正文或同源可信准入';
  END IF;
  IF (NEW.command->>'action'='create_plan'
    AND NEW.command->>'candidate_result_sha256' ~ '^[0-9a-f]{64}$'
    AND char_length(NEW.command->>'idempotency_key') BETWEEN 8 AND 100
    AND NEW.command->>'idempotency_key' ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'
    AND char_length(candidate_strip_text(NEW.command->>'reason')) BETWEEN 1 AND 1000
    AND NEW.command_sha256=encode(sha256(convert_to(
        candidate_canonical_json(NEW.command),'UTF8')),'hex')) IS NOT TRUE THEN
    RAISE EXCEPTION '候选规划命令缺少有界精确规范来源绑定';
  END IF;
  IF normalize_candidate_plan_command(NEW.raw_body) IS DISTINCT FROM NEW.command THEN
    RAISE EXCEPTION '候选规划命令原始正文与规范命令不一致';
  END IF;
  IF NEW.outcome IN ('applied','replayed') THEN
    SELECT * INTO plan FROM candidate_action_plans WHERE id=NEW.plan_id;
    IF (plan.id IS NOT NULL AND plan.investigation_run_id=NEW.investigation_run_id
      AND plan.candidate_job_id=NEW.candidate_job_id AND plan.requested_by_id=NEW.subject_id
      AND plan.command_sha256=NEW.command_sha256
      AND plan.candidate_result_sha256=NEW.command->>'candidate_result_sha256'
      AND COALESCE(to_jsonb(plan.wait_version),'null'::jsonb)=NEW.command->'expected_wait_version'
      AND to_jsonb(plan.incident_version)=NEW.command->'expected_incident_version'
      AND plan.idempotency_key=NEW.command->>'idempotency_key' AND plan.created_at<=NEW.created_at
      AND ((NEW.outcome='applied' AND plan.authorization_id=NEW.authorization_id
            AND plan.created_at=NEW.created_at)
        OR (NEW.outcome='replayed' AND plan.authorization_id<>NEW.authorization_id))) IS NOT TRUE
    THEN RAISE EXCEPTION '候选规划命令结果与原始草案不一致'; END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_plan_commands_guard BEFORE INSERT OR UPDATE OR DELETE
  ON candidate_plan_commands FOR EACH ROW EXECUTE FUNCTION guard_candidate_plan_command();
"""

_PLANS = """
CREATE FUNCTION assert_candidate_plan(plan candidate_action_plans) RETURNS VOID AS $$
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
    AND incident.id IS NOT NULL AND incident.status='investigating'
    AND incident.version=plan.incident_version AND job.status='succeeded'
    AND job.wait_version IS NOT DISTINCT FROM plan.wait_version) IS NOT TRUE THEN
    RAISE EXCEPTION '候选草案需要同运行成功候选、当前等待及事故版本';
  END IF;
  PERFORM assert_candidate_source(job,TRUE);
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

CREATE FUNCTION guard_candidate_plan() RETURNS trigger AS $$
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选草案不可更新或删除'; END IF;
  PERFORM assert_candidate_plan(NEW);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_action_plans_guard BEFORE INSERT OR UPDATE OR DELETE
  ON candidate_action_plans FOR EACH ROW EXECUTE FUNCTION guard_candidate_plan();
"""

_STATE = """
CREATE FUNCTION validate_candidate_plan_state() RETURNS trigger AS $$
DECLARE
  plan candidate_action_plans%ROWTYPE;
  audit candidate_plan_commands%ROWTYPE;
BEGIN
  IF TG_TABLE_NAME='candidate_action_plans' THEN
    SELECT * INTO plan FROM candidate_action_plans WHERE id=NEW.id;
  ELSIF NEW.outcome='applied' THEN
    SELECT * INTO plan FROM candidate_action_plans WHERE id=NEW.plan_id;
  ELSE RETURN NULL;
  END IF;
  SELECT * INTO audit FROM candidate_plan_commands WHERE authorization_id=plan.authorization_id;
  IF plan.id IS NULL OR audit.authorization_id IS NULL OR audit.outcome<>'applied'
    OR audit.plan_id IS DISTINCT FROM plan.id OR audit.created_at IS DISTINCT FROM plan.created_at
    OR audit.investigation_run_id IS DISTINCT FROM plan.investigation_run_id
    OR audit.candidate_job_id IS DISTINCT FROM plan.candidate_job_id
    OR audit.subject_id IS DISTINCT FROM plan.requested_by_id
    OR audit.command_sha256 IS DISTINCT FROM plan.command_sha256 THEN
    RAISE EXCEPTION '候选草案缺少同事务精确成功命令闭环';
  END IF;
  -- 只核验本次新草案；历史查询和精确重放不会重写已保存内容。
  PERFORM assert_candidate_plan(plan);
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER candidate_action_plans_state AFTER INSERT ON candidate_action_plans
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_candidate_plan_state();
CREATE CONSTRAINT TRIGGER candidate_plan_commands_state AFTER INSERT ON candidate_plan_commands
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_candidate_plan_state();
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM candidate_action_plans)
          OR EXISTS (SELECT 1 FROM candidate_plan_commands) THEN
          RAISE EXCEPTION '不能删除已保存的候选规划事实或命令审计';
        END IF;
      END $$;
      DROP TRIGGER candidate_action_plans_state ON candidate_action_plans;
      DROP TRIGGER candidate_plan_commands_state ON candidate_plan_commands;
      DROP FUNCTION validate_candidate_plan_state();
      DROP TRIGGER candidate_action_plans_guard ON candidate_action_plans;
      DROP FUNCTION guard_candidate_plan();
      DROP FUNCTION assert_candidate_plan(candidate_action_plans);
      DROP TRIGGER candidate_plan_commands_guard ON candidate_plan_commands;
      DROP FUNCTION guard_candidate_plan_command();
      DROP FUNCTION normalize_candidate_plan_command(TEXT);
      DROP TABLE candidate_plan_commands;
      DROP TABLE candidate_action_plans;
    """)
