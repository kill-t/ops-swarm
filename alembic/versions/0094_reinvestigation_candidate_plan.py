"""回流候选的可信单步骤规划准入与不可变命令审计。"""

# ruff: noqa: E501
from alembic import op

revision = "0094_reflow_candidate_plan"
down_revision = "0093_reflow_candidate"
branch_labels = None
depends_on = None

_TABLES = """
CREATE TABLE reinvestigation_candidate_plans (
  id UUID NOT NULL PRIMARY KEY,
  investigation_run_id UUID NOT NULL,
  candidate_job_id UUID NOT NULL,
  candidate_result_sha256 VARCHAR(64) NOT NULL,
  routing_sha256 VARCHAR(64) NOT NULL,
  credential_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
  incident_version INTEGER NOT NULL,
  policy_version VARCHAR(40) NOT NULL,
  requested_by_id VARCHAR(100) NOT NULL,
  authorization_id UUID NOT NULL REFERENCES operator_authorizations(id) ON DELETE RESTRICT,
  idempotency_key VARCHAR(100) NOT NULL,
  command_sha256 VARCHAR(64) NOT NULL,
  content JSONB NOT NULL,
  content_sha256 VARCHAR(64) NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  CONSTRAINT fk_reflow_plans_job_run FOREIGN KEY(candidate_job_id,investigation_run_id)
    REFERENCES investigation_reflow_candidate_jobs(id,investigation_run_id) ON DELETE RESTRICT,
  FOREIGN KEY(candidate_job_id) REFERENCES investigation_reflow_candidate_results(job_id) ON DELETE RESTRICT,
  CONSTRAINT uq_reflow_plans_job UNIQUE(candidate_job_id),
  CONSTRAINT uq_reflow_plans_id_run UNIQUE(id,investigation_run_id),
  CONSTRAINT uq_reflow_plans_authorization UNIQUE(authorization_id),
  CONSTRAINT uq_reflow_plans_key UNIQUE(investigation_run_id,requested_by_id,idempotency_key),
  CONSTRAINT ck_reflow_plans_policy CHECK (policy_version='reinvestigation-candidate-plan-v1'),
  CONSTRAINT ck_reflow_plans_identity CHECK (candidate_job_id=investigation_run_id),
  CONSTRAINT ck_reflow_plans_credential CHECK (isfinite(credential_expires_at) AND created_at<credential_expires_at),
  CONSTRAINT ck_reflow_plans_incident_version CHECK (incident_version>=1),
  CONSTRAINT ck_reflow_plans_hashes CHECK (routing_sha256 ~ '^[0-9a-f]{64}$' AND candidate_result_sha256 ~ '^[0-9a-f]{64}$'
    AND command_sha256 ~ '^[0-9a-f]{64}$' AND content_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_reflow_plans_content CHECK
    (jsonb_typeof(content)='object' AND octet_length(content::text)<=65536),
  CONSTRAINT ck_reflow_plans_subject CHECK
    (char_length(btrim(requested_by_id)) BETWEEN 1 AND 100),
  CONSTRAINT ck_reflow_plans_key CHECK (char_length(idempotency_key) BETWEEN 8 AND 100
    AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$')
);
CREATE TABLE reinvestigation_candidate_plan_commands (
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
  plan_id UUID REFERENCES reinvestigation_candidate_plans(id) ON DELETE RESTRICT,
  error_code VARCHAR(100),
  error_message VARCHAR(1000),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  credential_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
  CONSTRAINT ck_reflow_plan_commands_identity CHECK (candidate_job_id=investigation_run_id),
  CONSTRAINT ck_reflow_plan_commands_credential CHECK (isfinite(credential_expires_at) AND created_at<credential_expires_at),
  CONSTRAINT uq_reflow_plan_commands_authorization UNIQUE(authorization_id),
  CONSTRAINT ck_reflow_plan_commands_body CHECK
    (jsonb_typeof(command)='object' AND octet_length(command::text)<=32768),
  CONSTRAINT ck_reflow_plan_commands_raw CHECK (octet_length(raw_body) BETWEEN 2 AND 65536),
  CONSTRAINT ck_reflow_plan_commands_hash CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_reflow_plan_commands_subject CHECK
    (char_length(btrim(subject_id)) BETWEEN 1 AND 100),
  CONSTRAINT ck_reflow_plan_commands_outcome CHECK
    ((outcome IN ('applied','replayed') AND plan_id IS NOT NULL AND error_code IS NULL
      AND error_message IS NULL AND ((outcome='applied' AND http_status=201)
      OR (outcome='replayed' AND http_status=200)))
    OR (outcome='error' AND plan_id IS NULL AND error_code IS NOT NULL
      AND error_message IS NOT NULL AND http_status IN (404,409))),
  CONSTRAINT ck_reflow_plan_commands_error CHECK
    (error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,100}$')
);
CREATE INDEX ix_reflow_plan_commands_run_cursor
  ON reinvestigation_candidate_plan_commands(investigation_run_id,cursor);
"""

_COMMANDS = """
CREATE FUNCTION normalize_reflow_plan_command(raw_body TEXT) RETURNS JSONB AS $$
DECLARE body JSONB;
BEGIN
  body:=raw_body::jsonb;
  IF (jsonb_typeof(body)='object'
    AND body ?& ARRAY['candidate_result_sha256','routing_sha256',
                     'expected_incident_version','idempotency_key','reason']
    AND (body-ARRAY['action','candidate_result_sha256','routing_sha256',
                   'expected_incident_version','idempotency_key','reason'])='{}'::jsonb
    AND (NOT body ? 'action' OR (jsonb_typeof(body->'action')='string'
                                AND body->>'action'='create_plan'))
    AND jsonb_typeof(body->'candidate_result_sha256')='string'
    AND jsonb_typeof(body->'idempotency_key')='string'
    AND jsonb_typeof(body->'reason')='string'
    AND jsonb_typeof(body->'routing_sha256')='string'
    AND jsonb_typeof(body->'expected_incident_version')='number'
    AND raw_body::json->>'expected_incident_version' ~ '^[0-9]+$'
    AND (body->>'expected_incident_version')::numeric>=1) IS NOT TRUE THEN
    RETURN NULL;
  END IF;
  RETURN jsonb_build_object('action','create_plan',
    'candidate_result_sha256',candidate_strip_text(body->>'candidate_result_sha256'),
    'routing_sha256',candidate_strip_text(body->>'routing_sha256'),
    'expected_incident_version',body->'expected_incident_version',
    'idempotency_key',candidate_strip_text(body->>'idempotency_key'),
    'reason',candidate_strip_text(body->>'reason'));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
  RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

CREATE FUNCTION guard_reflow_plan_command() RETURNS trigger AS $$
DECLARE
  admission operator_authorizations%ROWTYPE;
  plan reinvestigation_candidate_plans%ROWTYPE;
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选规划命令审计不可更新或删除'; END IF;
  SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
  IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
    AND admission.role='engineer'
    AND admission.permissions ?& ARRAY['plan_action','manage_incident']
    AND admission.subject_id=NEW.subject_id AND admission.method='POST'
    AND admission.operation='create_reinvestigation_candidate_plan'
    AND admission.route=
      '/api/v1/investigations/{run_id}/reinvestigation-candidate/action-plan'
    AND admission.resources=jsonb_build_object('run_id',NEW.investigation_run_id::text)
    AND admission.authorized_at<=NEW.created_at AND isfinite(NEW.created_at)
    AND NEW.created_at<=clock_timestamp()+interval '1 second'
    AND NEW.created_at<NEW.credential_expires_at AND clock_timestamp()<NEW.credential_expires_at
    AND admission.body_sha256=encode(sha256(convert_to(NEW.raw_body,'UTF8')),'hex'))
    IS NOT TRUE THEN
    RAISE EXCEPTION '候选规划命令缺少精确原始正文或同源可信准入';
  END IF;
  IF (NEW.command->>'action'='create_plan'
    AND NEW.command->>'routing_sha256' ~ '^[0-9a-f]{64}$'
    AND NEW.command->>'candidate_result_sha256' ~ '^[0-9a-f]{64}$'
    AND char_length(NEW.command->>'idempotency_key') BETWEEN 8 AND 100
    AND NEW.command->>'idempotency_key' ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'
    AND char_length(candidate_strip_text(NEW.command->>'reason')) BETWEEN 1 AND 1000
    AND NEW.command_sha256=encode(sha256(convert_to(
        candidate_canonical_json(NEW.command),'UTF8')),'hex')) IS NOT TRUE THEN
    RAISE EXCEPTION '候选规划命令缺少有界精确规范来源绑定';
  END IF;
  IF normalize_reflow_plan_command(NEW.raw_body) IS DISTINCT FROM NEW.command THEN
    RAISE EXCEPTION '候选规划命令原始正文与规范命令不一致';
  END IF;
  IF NEW.outcome IN ('applied','replayed') THEN
    SELECT * INTO plan FROM reinvestigation_candidate_plans WHERE id=NEW.plan_id;
    IF (plan.id IS NOT NULL AND plan.investigation_run_id=NEW.investigation_run_id
      AND plan.candidate_job_id=NEW.candidate_job_id AND plan.requested_by_id=NEW.subject_id
      AND plan.command_sha256=NEW.command_sha256
      AND plan.candidate_result_sha256=NEW.command->>'candidate_result_sha256'
      AND plan.routing_sha256=NEW.command->>'routing_sha256'
      AND to_jsonb(plan.incident_version)=NEW.command->'expected_incident_version'
      AND plan.idempotency_key=NEW.command->>'idempotency_key' AND plan.created_at<=NEW.created_at
      AND ((NEW.outcome='applied' AND plan.authorization_id=NEW.authorization_id
            AND plan.created_at=NEW.created_at AND plan.credential_expires_at=NEW.credential_expires_at)
        OR (NEW.outcome='replayed' AND plan.authorization_id<>NEW.authorization_id))) IS NOT TRUE
    THEN RAISE EXCEPTION '候选规划命令结果与原始草案不一致'; END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER reflow_plan_commands_guard BEFORE INSERT OR UPDATE OR DELETE
  ON reinvestigation_candidate_plan_commands FOR EACH ROW EXECUTE FUNCTION guard_reflow_plan_command();
"""

_SOURCE = """
CREATE FUNCTION reflow_plan_candidate(run_id UUID,created TIMESTAMPTZ) RETURNS JSONB AS $$
DECLARE job investigation_reflow_candidate_jobs%ROWTYPE;
 result investigation_reflow_candidate_results%ROWTYPE;
 publisher investigation_reflow_candidate_attempts%ROWTYPE;
 caller investigation_reflow_candidate_attempts%ROWTYPE;
 call investigation_call_reservations%ROWTYPE; expected JSONB;
BEGIN
 SELECT * INTO job FROM investigation_reflow_candidate_jobs WHERE id=run_id;
 IF job.status IS DISTINCT FROM 'succeeded' THEN
 RAISE EXCEPTION '回流规划必须引用完整成功候选'; END IF;
 PERFORM assert_reflow_candidate_source(job,FALSE);
 SELECT * INTO result FROM investigation_reflow_candidate_results WHERE job_id=job.id;
 SELECT * INTO publisher FROM investigation_reflow_candidate_attempts WHERE id=result.attempt_id;
 SELECT * INTO call FROM investigation_call_reservations WHERE id=result.source_call_id;
 SELECT * INTO caller FROM investigation_reflow_candidate_attempts WHERE id=call.reflow_candidate_attempt_id;
 IF (job.id=run_id AND job.investigation_run_id=run_id
   AND result.job_id=job.id AND result.investigation_run_id=run_id
   AND publisher.job_id=job.id AND publisher.investigation_run_id=run_id
   AND publisher.status='succeeded' AND publisher.number=job.attempt_count
   AND publisher.finished_at=result.created_at AND job.completed_at=result.created_at
   AND result.created_at>=publisher.started_at AND result.created_at<publisher.lease_expires_at
   AND result.created_at<=created AND isfinite(created)
   AND (SELECT count(*) FROM investigation_reflow_candidate_attempts WHERE job_id=job.id)=job.attempt_count
   AND NOT EXISTS(SELECT 1 FROM investigation_reflow_candidate_attempts WHERE job_id=job.id
     AND number<job.attempt_count AND status IN ('running','succeeded'))
   AND caller.job_id=job.id AND caller.investigation_run_id=run_id
   AND caller.number<=publisher.number AND caller.status IN ('succeeded','failed','lease_expired')
   AND caller.started_at>=job.created_at AND caller.finished_at>=call.finished_at
   AND caller.finished_at<=result.created_at
   AND call.investigation_run_id=run_id AND call.investigation_attempt_id::text=job.source->>'parent_attempt_id'
   AND call.operation='evaluate_reinvestigation_candidate' AND call.kind='model' AND call.status='succeeded'
   AND call.error_code IS NULL AND call.invocation_index=0
   AND call.idempotency_key='reflow-candidate:'||caller.id::text
   AND call.reserved_at>=caller.started_at AND call.finished_at>=call.reserved_at
   AND call.finished_at<caller.lease_expires_at AND call.finished_at<=result.created_at
   AND call.task_id IS NULL AND call.task_attempt_id IS NULL AND call.human_review_attempt_id IS NULL
   AND call.candidate_attempt_id IS NULL AND call.reinvestigation_attempt_id IS NULL
   AND call.reflow_task_id IS NULL AND call.reflow_task_attempt_id IS NULL
   AND call.reflow_synthesis_attempt_id IS NULL AND call.reflow_review_attempt_id IS NULL
   AND call.tool_request IS NULL AND call.model_input=job.packet->'model_input'
   AND call.model_allocation=job.packet->'allocation' AND call.context_manifest=job.packet->'manifest'
   AND call.call_result=jsonb_build_object('kind','model','version','candidate-model-result-v1',
     'proposal',call.call_result->'proposal')) IS NOT TRUE THEN
 RAISE EXCEPTION '回流规划缺少同源候选、真实成功调用者与发布尝试闭包'; END IF;
 IF call.model_usage IS NOT NULL AND
   ((call.model_usage->>'input_tokens')::bigint>(call.model_allocation->>'estimated_input_tokens')::bigint
   OR (call.model_usage->>'output_tokens')::bigint>(call.model_allocation->>'output_token_limit')::bigint) THEN
 RAISE EXCEPTION '超原分配的候选结果不能进入规划'; END IF;
 expected:=jsonb_build_object('version','reinvestigation-candidate-result-v1',
   'investigation_run_id',run_id::text,'job_id',job.id::text,'source_call_id',call.id::text,
   'source_sha256',job.source_sha256,'routing_sha256',job.source->'routing_sha256',
   'evaluation',reflow_candidate_evaluation(call.call_result->'proposal',job.source),
   'status','recorded','created_at',candidate_recovery_utc_text(result.created_at));
 IF result.content IS DISTINCT FROM expected OR result.content_sha256 IS DISTINCT FROM reflow_hash(expected)
 THEN RAISE EXCEPTION '回流规划候选外层完整正文或摘要无法从真实来源重建'; END IF;
 RETURN expected;
END; $$ LANGUAGE plpgsql;

CREATE FUNCTION reflow_plan_content(run_id UUID,created TIMESTAMPTZ) RETURNS JSONB AS $$
DECLARE candidate JSONB:=reflow_plan_candidate(run_id,created); source JSONB; evaluation JSONB:=candidate->'evaluation';
BEGIN
 SELECT job.source INTO source FROM investigation_reflow_candidate_jobs job WHERE id=run_id;
 IF evaluation->>'disposition'<>'planning_review' OR evaluation->'step'='null'::jsonb THEN
 RAISE EXCEPTION '不适用或参数不足的回流候选不能生成草案'; END IF;
 RETURN jsonb_build_object('version','reinvestigation-candidate-plan-content-v1',
   'candidate_job_id',run_id::text,'candidate_result_sha256',reflow_hash(candidate),
   'candidate_source_sha256',candidate->'source_sha256','routing_sha256',source->'routing_sha256',
   'review_result_sha256',source#>'{routing,review_result_sha256}',
   'synthesis_result_sha256',source#>'{routing,synthesis_result_sha256}',
   'hypothesis',source#>'{review_source,synthesis_result,proposal}',
   'evidence_ids',evaluation#>'{proposal,evidence_ids}',
   'preconditions',evaluation#>'{proposal,preconditions}',
   'candidate_unresolved_questions',evaluation#>'{proposal,unresolved_questions}',
   'observation_windows',source#>'{routing,observation_windows}',
   'unresolved_constraints',source#>'{routing,unresolved_constraints}',
   'approval_eligible',FALSE,'execution_eligible',FALSE,
   'plan',jsonb_build_object('version',1,'status','draft',
     'summary','基于已记录候选生成的单步骤处置草案，需另行可信审批。',
     'risk_level','high','requires_human_approval',TRUE,'risk_policy_version','action-risk-v3',
     'steps',jsonb_build_array(evaluation->'step')));
END; $$ LANGUAGE plpgsql;
"""

_PLANS = """
CREATE FUNCTION assert_reflow_plan(plan reinvestigation_candidate_plans,require_current BOOLEAN DEFAULT TRUE)
 RETURNS VOID AS $$
DECLARE job investigation_reflow_candidate_jobs%ROWTYPE; admission operator_authorizations%ROWTYPE;
 run investigation_runs%ROWTYPE; incident incidents%ROWTYPE; expected JSONB; reason TEXT;
BEGIN
 IF require_current THEN
   PERFORM 1 FROM work_items WHERE investigation_run_id=plan.investigation_run_id FOR UPDATE;
   PERFORM 1 FROM investigation_reflow_candidate_jobs WHERE id=plan.candidate_job_id FOR UPDATE;
   PERFORM lock_reflow_candidate_basis(plan.investigation_run_id);
 END IF;
 SELECT * INTO run FROM investigation_runs WHERE id=plan.investigation_run_id;
 SELECT * INTO job FROM investigation_reflow_candidate_jobs WHERE id=plan.candidate_job_id;
 expected:=reflow_plan_content(plan.investigation_run_id,plan.created_at);
 IF (plan.candidate_job_id=plan.investigation_run_id AND job.id=run.id
   AND plan.policy_version='reinvestigation-candidate-plan-v1'
   AND plan.candidate_result_sha256=expected->>'candidate_result_sha256'
   AND plan.routing_sha256=expected->>'routing_sha256'
   AND plan.content=expected AND plan.content_sha256=reflow_hash(expected)
   AND isfinite(plan.created_at) AND isfinite(plan.credential_expires_at)
   AND plan.created_at<plan.credential_expires_at) IS NOT TRUE THEN
 RAISE EXCEPTION '回流草案不能替换完整新来源、动作、参数或审批执行资格'; END IF;
 SELECT * INTO admission FROM operator_authorizations WHERE id=plan.authorization_id;
 IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
   AND admission.role='engineer' AND admission.permissions ?& ARRAY['plan_action','manage_incident']
   AND admission.subject_id=plan.requested_by_id AND admission.method='POST'
   AND admission.operation='create_reinvestigation_candidate_plan'
   AND admission.route='/api/v1/investigations/{run_id}/reinvestigation-candidate/action-plan'
   AND admission.resources=jsonb_build_object('run_id',run.id::text)
   AND admission.authorized_at<=plan.created_at) IS NOT TRUE THEN
 RAISE EXCEPTION '回流草案缺少同源可信工程师准入'; END IF;
 IF require_current THEN
   SELECT * INTO incident FROM incidents WHERE id=run.incident_id FOR UPDATE;
   IF (incident.status='investigating' AND incident.version=plan.incident_version
     AND plan.created_at<=clock_timestamp()+interval '1 second'
     AND clock_timestamp()<plan.credential_expires_at
     AND reflow_candidate_basis_blocker(run.id,plan.created_at) IS NULL
     AND reflow_candidate_basis_blocker(run.id,clock_timestamp()) IS NULL) IS NOT TRUE THEN
   RAISE EXCEPTION '回流规划事故版本、精确依据、总期限或凭据有效期已失效'; END IF;
   PERFORM assert_reflow_candidate_source(job,TRUE);
   -- 确定性规划不预留新调用，容量恰好到顶仍可消费已有成功候选。
   reason:=reflow_candidate_budget_blocker(run.id,NULL,NULL);
   IF reason IS NOT NULL THEN RAISE EXCEPTION '回流规划原累计账本拒绝准入: %',reason; END IF;
 END IF;
END; $$ LANGUAGE plpgsql;

CREATE FUNCTION guard_reflow_plan() RETURNS trigger AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '回流规划草案不可更新或删除'; END IF;
 PERFORM assert_reflow_plan(NEW,TRUE);
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER reflow_plans_guard BEFORE INSERT OR UPDATE OR DELETE ON reinvestigation_candidate_plans
 FOR EACH ROW EXECUTE FUNCTION guard_reflow_plan();
"""

_STATE = """
CREATE FUNCTION validate_reflow_plan_state() RETURNS trigger AS $$
DECLARE plan reinvestigation_candidate_plans%ROWTYPE;
 audit reinvestigation_candidate_plan_commands%ROWTYPE; plan_tx XID; audit_tx XID;
BEGIN
 IF TG_TABLE_NAME='reinvestigation_candidate_plans' THEN
   SELECT * INTO plan FROM reinvestigation_candidate_plans WHERE id=NEW.id;
 ELSE
   SELECT * INTO audit FROM reinvestigation_candidate_plan_commands WHERE cursor=NEW.cursor;
   -- 每份新命令使用本次服务端凭据截止；历史读取不重新判定已过期凭据。
   IF clock_timestamp()>=audit.credential_expires_at THEN
   RAISE EXCEPTION '回流规划命令提交前操作者凭据已过期'; END IF;
   IF audit.outcome<>'applied' THEN RETURN NULL; END IF;
   SELECT * INTO plan FROM reinvestigation_candidate_plans WHERE id=audit.plan_id;
 END IF;
 SELECT * INTO audit FROM reinvestigation_candidate_plan_commands WHERE authorization_id=plan.authorization_id;
 IF (plan.id IS NOT NULL AND audit.authorization_id IS NOT NULL AND audit.outcome='applied'
   AND audit.plan_id=plan.id AND audit.created_at=plan.created_at
   AND audit.investigation_run_id=plan.investigation_run_id AND audit.candidate_job_id=plan.candidate_job_id
   AND audit.subject_id=plan.requested_by_id AND audit.command_sha256=plan.command_sha256
   AND audit.credential_expires_at=plan.credential_expires_at) IS NOT TRUE THEN
 RAISE EXCEPTION '回流草案缺少精确同源成功命令闭环'; END IF;
 SELECT xmin INTO plan_tx FROM reinvestigation_candidate_plans WHERE id=plan.id;
 SELECT xmin INTO audit_tx FROM reinvestigation_candidate_plan_commands WHERE cursor=audit.cursor;
 IF plan_tx IS DISTINCT FROM audit_tx THEN RAISE EXCEPTION '回流草案和成功审计必须同事务提交'; END IF;
 PERFORM assert_reflow_plan(plan,TRUE);
 RETURN NULL;
END; $$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER reflow_plans_state AFTER INSERT ON reinvestigation_candidate_plans
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_reflow_plan_state();
CREATE CONSTRAINT TRIGGER reflow_plan_commands_state AFTER INSERT ON reinvestigation_candidate_plan_commands
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_reflow_plan_state();
"""


def upgrade() -> None:
    for sql in (_TABLES, _COMMANDS, _SOURCE, _PLANS, _STATE):
        op.execute(sql)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM reinvestigation_candidate_plans)
        OR EXISTS(SELECT 1 FROM reinvestigation_candidate_plan_commands) THEN
      RAISE EXCEPTION '不能删除已保存的回流规划事实或命令审计'; END IF;
    END $$;
    DROP FUNCTION validate_reflow_plan_state() CASCADE;
    DROP FUNCTION guard_reflow_plan() CASCADE;
    DROP FUNCTION assert_reflow_plan(reinvestigation_candidate_plans,BOOLEAN);
    DROP FUNCTION reflow_plan_content(UUID,TIMESTAMPTZ);
    DROP FUNCTION reflow_plan_candidate(UUID,TIMESTAMPTZ);
    DROP FUNCTION guard_reflow_plan_command() CASCADE;
    DROP FUNCTION normalize_reflow_plan_command(TEXT);
    DROP TABLE reinvestigation_candidate_plan_commands;
    DROP TABLE reinvestigation_candidate_plans;
    """)
