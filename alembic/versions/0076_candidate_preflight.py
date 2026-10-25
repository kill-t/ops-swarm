"""候选执行前只读观测、原始命令与不可变历史的原生守卫。"""

from alembic import op

revision = "0076_candidate_preflight"
down_revision = "0075_candidate_decision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(_COMMANDS)
    op.execute(_PREFLIGHTS)
    op.execute(_STATE)


_TABLES = """
CREATE TABLE candidate_execution_preflights (
 id UUID NOT NULL PRIMARY KEY,
 cursor BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL,
 operation_id UUID NOT NULL,
 investigation_run_id UUID NOT NULL,
 candidate_job_id UUID NOT NULL,
 decision_id UUID NOT NULL REFERENCES candidate_approval_decisions(id) ON DELETE RESTRICT,
 decision_content_sha256 VARCHAR(64) NOT NULL,
 incident_version INTEGER NOT NULL,
 policy_version VARCHAR(40) NOT NULL,
 requested_by_id VARCHAR(100) NOT NULL,
 authorization_id UUID NOT NULL REFERENCES operator_authorizations(id) ON DELETE RESTRICT,
 idempotency_key VARCHAR(100) NOT NULL,
 command_sha256 VARCHAR(64) NOT NULL,
 content JSONB NOT NULL,
 content_sha256 VARCHAR(64) NOT NULL,
 created_at TIMESTAMP WITH TIME ZONE NOT NULL,
 expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
 CONSTRAINT fk_candidate_preflight_job_run FOREIGN KEY(candidate_job_id,investigation_run_id)
 REFERENCES investigation_candidate_evaluation_jobs(id,investigation_run_id) ON DELETE RESTRICT,
 CONSTRAINT uq_candidate_preflight_cursor UNIQUE(cursor),
 CONSTRAINT uq_candidate_preflight_authorization UNIQUE(authorization_id),
 CONSTRAINT uq_candidate_preflight_key UNIQUE(investigation_run_id,requested_by_id,idempotency_key),
 CONSTRAINT ck_candidate_preflight_policy CHECK (policy_version='candidate-execution-preflight-v1'),
 CONSTRAINT ck_candidate_preflight_version CHECK (incident_version>=2),
 CONSTRAINT ck_candidate_preflight_hashes CHECK (decision_content_sha256 ~ '^[0-9a-f]{64}$'
   AND command_sha256 ~ '^[0-9a-f]{64}$' AND content_sha256 ~ '^[0-9a-f]{64}$'),
 CONSTRAINT ck_candidate_preflight_content CHECK
   (jsonb_typeof(content)='object' AND octet_length(content::text)<=65536),
 CONSTRAINT ck_candidate_preflight_subject CHECK
   (char_length(btrim(requested_by_id)) BETWEEN 1 AND 100),
 CONSTRAINT ck_candidate_preflight_key CHECK (char_length(idempotency_key) BETWEEN 8 AND 100
   AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'),
 CONSTRAINT ck_candidate_preflight_expiry CHECK
   (expires_at>created_at AND expires_at<=created_at+interval '30 seconds')
);
CREATE INDEX ix_candidate_preflight_run_job_cursor ON candidate_execution_preflights
 (investigation_run_id,candidate_job_id,cursor);
CREATE TABLE candidate_preflight_commands (
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
  preflight_id UUID REFERENCES candidate_execution_preflights(id) ON DELETE RESTRICT,
  error_code VARCHAR(100),
  error_message VARCHAR(1000),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  CONSTRAINT uq_candidate_preflight_commands_authorization UNIQUE(authorization_id),
  CONSTRAINT ck_candidate_preflight_commands_body CHECK
    (jsonb_typeof(command)='object' AND octet_length(command::text)<=32768),
  CONSTRAINT ck_candidate_preflight_commands_raw CHECK
    (octet_length(raw_body) BETWEEN 2 AND 65536),
  CONSTRAINT ck_candidate_preflight_commands_hash CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_candidate_preflight_commands_subject CHECK
    (char_length(btrim(subject_id)) BETWEEN 1 AND 100),
  CONSTRAINT ck_candidate_preflight_commands_outcome CHECK
    ((outcome IN ('applied','replayed') AND preflight_id IS NOT NULL AND error_code IS NULL
      AND error_message IS NULL AND ((outcome='applied' AND http_status=201)
      OR (outcome='replayed' AND http_status=200)))
    OR (outcome='error' AND preflight_id IS NULL AND error_code IS NOT NULL
      AND error_message IS NOT NULL AND http_status IN (404,409))),
  CONSTRAINT ck_candidate_preflight_commands_error CHECK
    (error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,100}$')
);
CREATE INDEX ix_candidate_preflight_commands_run_cursor
  ON candidate_preflight_commands(investigation_run_id,cursor);
"""

_COMMANDS = """

CREATE FUNCTION normalize_candidate_preflight_command(raw_body TEXT) RETURNS JSONB AS $$
DECLARE body JSONB; decision_uuid UUID; decision_text TEXT; uuid_pattern TEXT;
BEGIN
  body:=raw_body::jsonb;
  IF (jsonb_typeof(body)='object'
    AND body ?& ARRAY['decision_id','decision_content_sha256',
                     'expected_incident_version','idempotency_key','reason']
    AND (body-ARRAY['decision_id','decision_content_sha256',
                   'expected_incident_version','idempotency_key','reason'])='{}'::jsonb
    AND jsonb_typeof(body->'decision_id')='string'
    AND jsonb_typeof(body->'decision_content_sha256')='string'
    AND jsonb_typeof(body->'idempotency_key')='string'
    AND jsonb_typeof(body->'reason')='string'
    AND jsonb_typeof(body->'expected_incident_version')='number'
    AND raw_body::json->>'expected_incident_version' ~ '^[0-9]+$'
    AND (body->>'expected_incident_version')::numeric>=2) IS NOT TRUE THEN
    RETURN NULL;
  END IF;
  decision_text:=body->>'decision_id';
  uuid_pattern:='[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-' ||
                '[0-9a-fA-F]{4}-[0-9a-fA-F]{12}';
  -- UUID 字段不使用字符串裁剪；只接受 Pydantic 允许的四类文本形式。
  IF decision_text ~ ('^urn:uuid:'||uuid_pattern||'$') THEN
    decision_text:=substr(decision_text,10);
  ELSIF decision_text !~ ('^('||uuid_pattern||'|[0-9a-fA-F]{32}|[{]'||
                         uuid_pattern||'[}])$') THEN
    RETURN NULL;
  END IF;
  decision_uuid:=decision_text::uuid;
  RETURN jsonb_build_object('decision_id',decision_uuid::text,
    'decision_content_sha256',candidate_strip_text(body->>'decision_content_sha256'),
    'expected_incident_version',body->'expected_incident_version',
    'idempotency_key',candidate_strip_text(body->>'idempotency_key'),
    'reason',candidate_strip_text(body->>'reason'));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
  RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

CREATE FUNCTION guard_candidate_preflight_command() RETURNS trigger AS $$
DECLARE admission operator_authorizations%ROWTYPE; preflight candidate_execution_preflights%ROWTYPE;
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选检查命令审计不可更新或删除'; END IF;
  SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id FOR UPDATE;
  IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
    AND admission.role='executor' AND admission.permissions ? 'execute_action'
    AND admission.subject_id=NEW.subject_id AND admission.method='POST'
    AND admission.operation='check_candidate_execution'
    AND admission.route=
      '/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-preflights'
    AND admission.resources=jsonb_build_object('run_id',NEW.investigation_run_id::text,
                                              'job_id',NEW.candidate_job_id::text)
    AND admission.authorized_at<=NEW.created_at
    AND admission.body_sha256=encode(sha256(convert_to(NEW.raw_body,'UTF8')),'hex'))
    IS NOT TRUE THEN RAISE EXCEPTION '候选检查命令缺少精确原始正文或同源可信准入'; END IF;
  IF (NEW.command->>'decision_content_sha256' ~ '^[0-9a-f]{64}$'
    AND char_length(NEW.command->>'idempotency_key') BETWEEN 8 AND 100
    AND NEW.command->>'idempotency_key' ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'
    AND char_length(candidate_strip_text(NEW.command->>'reason')) BETWEEN 1 AND 500
    AND NEW.command_sha256=encode(sha256(convert_to(
      candidate_canonical_json(NEW.command),'UTF8')),'hex')) IS NOT TRUE THEN
    RAISE EXCEPTION '候选检查命令缺少有界精确规范来源绑定';
  END IF;
  IF normalize_candidate_preflight_command(NEW.raw_body) IS DISTINCT FROM NEW.command THEN
    RAISE EXCEPTION '候选检查命令原始正文与规范命令不一致';
  END IF;
  IF NEW.outcome='error' AND EXISTS (
    SELECT 1 FROM candidate_execution_preflights p
    WHERE p.investigation_run_id=NEW.investigation_run_id AND p.requested_by_id=NEW.subject_id
      AND p.candidate_job_id=NEW.candidate_job_id
      AND p.idempotency_key=NEW.command->>'idempotency_key'
      AND p.command_sha256=NEW.command_sha256) THEN
    RAISE EXCEPTION '已有候选检查事实不能降级为业务拒绝审计';
  END IF;
  IF NEW.outcome IN ('applied','replayed') THEN
    SELECT * INTO preflight FROM candidate_execution_preflights WHERE id=NEW.preflight_id;
    IF (preflight.id IS NOT NULL AND preflight.investigation_run_id=NEW.investigation_run_id
      AND preflight.candidate_job_id=NEW.candidate_job_id
      AND preflight.requested_by_id=NEW.subject_id AND preflight.command_sha256=NEW.command_sha256
      AND preflight.decision_id::text=NEW.command->>'decision_id'
      AND preflight.decision_content_sha256=NEW.command->>'decision_content_sha256'
      AND to_jsonb(preflight.incident_version)=NEW.command->'expected_incident_version'
      AND preflight.idempotency_key=NEW.command->>'idempotency_key'
      AND preflight.created_at<=NEW.created_at
      AND ((NEW.outcome='applied' AND preflight.authorization_id=NEW.authorization_id
        AND preflight.created_at=NEW.created_at)
        OR (NEW.outcome='replayed' AND preflight.authorization_id<>NEW.authorization_id)))
      IS NOT TRUE THEN RAISE EXCEPTION '候选检查命令结果与原始检查不一致'; END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_preflight_commands_guard BEFORE INSERT OR UPDATE OR DELETE
  ON candidate_preflight_commands FOR EACH ROW EXECUTE FUNCTION guard_candidate_preflight_command();
"""

_PREFLIGHTS = """
CREATE FUNCTION assert_candidate_execution_preflight(preflight candidate_execution_preflights)
RETURNS VOID AS $$
DECLARE
  decision candidate_approval_decisions%ROWTYPE;
  request candidate_approval_requests%ROWTYPE;
  plan candidate_action_plans%ROWTYPE;
  job investigation_candidate_evaluation_jobs%ROWTYPE;
  run investigation_runs%ROWTYPE;
  incident incidents%ROWTYPE;
  event incident_events%ROWTYPE;
  admission operator_authorizations%ROWTYPE;
  decision_admission operator_authorizations%ROWTYPE;
  audit candidate_decision_commands%ROWTYPE;
  current_version INTEGER;
  expected_content JSONB;
  observation JSONB;
  step JSONB;
  blockers JSONB;
  observed_at TIMESTAMP WITH TIME ZONE;
  action TEXT;
  expected_path TEXT;
BEGIN
  SELECT * INTO admission FROM operator_authorizations
    WHERE id=preflight.authorization_id FOR UPDATE;
  PERFORM 1 FROM work_items WHERE investigation_run_id=preflight.investigation_run_id FOR UPDATE;
  SELECT * INTO job FROM investigation_candidate_evaluation_jobs
    WHERE id=preflight.candidate_job_id FOR UPDATE;
  SELECT * INTO run FROM investigation_runs WHERE id=preflight.investigation_run_id;
  SELECT * INTO incident FROM incidents WHERE id=run.incident_id FOR UPDATE;
  SELECT * INTO decision FROM candidate_approval_decisions WHERE id=preflight.decision_id;
  SELECT * INTO request FROM candidate_approval_requests WHERE id=decision.request_id;
  SELECT * INTO plan FROM candidate_action_plans WHERE id=request.plan_id;
  IF (decision.id IS NOT NULL AND request.id IS NOT NULL
    AND decision.outcome='approved' AND decision.investigation_run_id=run.id
    AND decision.candidate_job_id=job.id AND job.investigation_run_id=run.id
    AND request.investigation_run_id=run.id AND request.candidate_job_id=job.id
    AND decision.plan_content_sha256=request.plan_content_sha256
    AND decision.previous_incident_version=request.incident_version
    AND decision.incident_version=request.incident_version+1
    AND preflight.decision_content_sha256=decision.content_sha256
    AND incident.status='mitigating' AND incident.version=preflight.incident_version
    AND preflight.incident_version=decision.incident_version
    AND request.created_at<=decision.created_at AND decision.created_at<=preflight.created_at
    AND preflight.created_at<request.expires_at AND decision.created_at<request.expires_at
    AND preflight.created_at<run.started_at+
      make_interval(secs=>(run.call_budget->>'max_duration_seconds')::int)) IS NOT TRUE THEN
    RAISE EXCEPTION '候选检查需要精确当前批准、事故状态版本及原有效期限';
  END IF;
  -- 历史授权只读；不能锁定旧准入，不能要求旧决定时的 incident.updated_at。
  -- 完整来源含浮点观测，其散列及 UUID5 身份由应用重建，不新增数据库散列扩展。
  PERFORM assert_candidate_decision_request_history(request,TRUE);
  SELECT max(state_version) INTO current_version FROM investigation_human_wait_events
    WHERE investigation_run_id=run.id;
  IF (job.source_kind='terminal_handoff' AND run.status='await_candidate') IS NOT TRUE
    AND (job.source_kind='human_route' AND run.status='await_human'
      AND current_version IS NOT DISTINCT FROM request.wait_version
      AND NOT EXISTS (SELECT 1 FROM investigation_human_wait_events h
        WHERE h.investigation_run_id=run.id AND h.action='cancel')) IS NOT TRUE THEN
    RAISE EXCEPTION '候选检查的原输入已补充、取消或不再适用';
  END IF;
  SELECT * INTO decision_admission FROM operator_authorizations WHERE id=decision.authorization_id;
  SELECT * INTO audit FROM candidate_decision_commands
    WHERE authorization_id=decision.authorization_id;
  IF (decision_admission.policy_version='operator-auth-v1'
    AND decision_admission.role='approver' AND decision_admission.permissions ? 'decide_approval'
    AND decision_admission.subject_id=decision.decided_by_id AND decision_admission.method='POST'
    AND decision_admission.operation='decide_candidate_approval'
    AND decision_admission.route=
      '/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan/approval-request/decision'
    AND decision_admission.resources=jsonb_build_object('run_id',run.id::text,'job_id',job.id::text)
    AND decision_admission.authorized_at<=decision.created_at
    AND audit.outcome='applied' AND audit.http_status=201 AND audit.decision_id=decision.id
    AND audit.investigation_run_id=run.id AND audit.candidate_job_id=job.id
    AND audit.subject_id=decision.decided_by_id AND audit.created_at=decision.created_at
    AND audit.command_sha256=decision.command_sha256
    AND normalize_candidate_decision_command(audit.raw_body)=audit.command
    AND audit.command_sha256=encode(sha256(convert_to(
      candidate_canonical_json(audit.command),'UTF8')),'hex')
    AND decision_admission.body_sha256=encode(sha256(convert_to(audit.raw_body,'UTF8')),'hex')
    AND audit.command->>'action'='approve' AND audit.command->>'request_id'=request.id::text
    AND audit.command->>'plan_content_sha256'=decision.plan_content_sha256
    AND audit.command->'expected_incident_version'=to_jsonb(decision.previous_incident_version)
    AND audit.command->>'idempotency_key'=decision.idempotency_key) IS NOT TRUE THEN
    RAISE EXCEPTION '候选检查缺少完整原批准可信命令历史';
  END IF;
  expected_content:=jsonb_build_object('version','candidate-approval-decision-content-v1',
    'request_id',request.id::text,'plan_id',plan.id::text,
    'plan_content_sha256',plan.content_sha256,'candidate_job_id',job.id::text,
    'source_content_sha256',job.source_content_sha256,'wait_version',request.wait_version,
    'outcome','approved','evidence_ids',plan.content->'evidence_ids',
    'preconditions',plan.content->'preconditions',
    'unresolved_questions',plan.content->'unresolved_questions','execution_eligible',FALSE);
  IF decision.content IS DISTINCT FROM expected_content OR decision.content_sha256 IS DISTINCT FROM
    encode(sha256(convert_to(candidate_canonical_json(decision.content),'UTF8')),'hex') THEN
    RAISE EXCEPTION '候选检查的原批准内容或摘要不完整';
  END IF;
  SELECT * INTO event FROM incident_events WHERE id=decision.event_cursor;
  IF (event.incident_id=incident.id AND event.event_type='incident.status_changed'
    AND event.actor_type='human' AND event.actor_id=decision.decided_by_id
    AND event.from_status='awaiting_approval' AND event.to_status='mitigating'
    AND event.incident_version=decision.incident_version AND event.occurred_at=decision.created_at
    AND event.payload=jsonb_build_object('action','approve_plan')
    AND event.reason=audit.command->>'reason') IS NOT TRUE THEN
    RAISE EXCEPTION '候选检查缺少精确原批准迁移事件';
  END IF;
  IF (admission.policy_version='operator-auth-v1' AND admission.role='executor'
    AND admission.permissions ? 'execute_action' AND admission.subject_id=preflight.requested_by_id
    AND admission.method='POST' AND admission.operation='check_candidate_execution'
    AND admission.route=
      '/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-preflights'
    AND admission.resources=jsonb_build_object('run_id',run.id::text,'job_id',job.id::text)
    AND admission.authorized_at<=preflight.created_at) IS NOT TRUE THEN
    RAISE EXCEPTION '候选检查缺少同源可信执行者准入';
  END IF;
  step:=plan.content #> '{plan,steps,0}';
  action:=step->>'action_type';
  expected_path:=CASE action
    WHEN 'rollback_release' THEN '/__admin/faults/release-500'
    WHEN 'rollback_configuration' THEN '/__admin/configuration/database-pool'
    WHEN 'restore_dependency' THEN '/__admin/dependencies/redis' ELSE NULL END;
  IF (expected_path IS NOT NULL AND
      ((action IN ('rollback_release','rollback_configuration')
        AND step->>'target_type'='service' AND step->>'target_name'='order-service')
       OR (action='restore_dependency' AND step->>'target_type'='dependency'
        AND step->>'target_name'='redis'))) IS NOT TRUE THEN
    RAISE EXCEPTION '候选检查动作及目标不在本地只读白名单';
  END IF;
  observation:=preflight.content->'observation';
  IF (jsonb_typeof(observation)='object'
    AND observation ?& ARRAY['action_type','observed_at','source_path','current_value',
                             'available','release_enabled','error_code']
    AND (observation-ARRAY['action_type','observed_at','source_path','current_value',
                         'available','release_enabled','error_code'])='{}'::jsonb
    AND observation->>'action_type'=action AND observation->>'source_path'=expected_path
    AND jsonb_typeof(observation->'observed_at')='string'
    AND observation->>'observed_at' ~ '(Z|[+-][0-9]{2}:[0-9]{2})$') IS NOT TRUE THEN
    RAISE EXCEPTION '候选检查观测必须绑定固定动作、路径及精确有界形状';
  END IF;
  observed_at:=(observation->>'observed_at')::timestamptz;
  IF (observed_at>=decision.created_at AND observed_at<=preflight.created_at
    AND preflight.expires_at=least(request.expires_at,observed_at+interval '30 seconds')
    AND preflight.created_at<preflight.expires_at) IS NOT TRUE THEN
    RAISE EXCEPTION '候选检查观测时间不能倒置、过期或续期';
  END IF;
  blockers:='["unverified_preconditions"]'::jsonb;
  IF jsonb_array_length(plan.content->'unresolved_questions')>0 THEN
    blockers:=blockers||'"unresolved_questions"'::jsonb;
  END IF;
  IF action='rollback_release' THEN
    blockers:=blockers||'"runtime_version_unverifiable"'::jsonb;
  END IF;
  IF observation->'error_code'<>'null'::jsonb THEN
    IF (jsonb_typeof(observation->'error_code')='string'
      AND char_length(observation->>'error_code') BETWEEN 1 AND 100
      AND observation->>'error_code' ~ '^[a-z][a-z0-9_]*$'
      AND observation->'current_value'='null'::jsonb
      AND observation->'available'='null'::jsonb
      AND observation->'release_enabled'='null'::jsonb) IS NOT TRUE THEN
      RAISE EXCEPTION '候选检查失败观测不能夹带参数';
    END IF;
    blockers:=blockers||'"runtime_observation_failed"'::jsonb;
  ELSIF action='rollback_configuration' THEN
    IF (jsonb_typeof(observation->'current_value')='number'
      AND observation->>'current_value' ~ '^[0-9]+$'
      AND (observation->>'current_value')::numeric BETWEEN 1 AND 1000
      AND observation->'available'='null'::jsonb
      AND observation->'release_enabled'='null'::jsonb) IS NOT TRUE THEN
      RAISE EXCEPTION '候选检查配置观测必须为唯一严格整数参数';
    END IF;
    IF observation->'current_value'=step #> '{parameters,target_value}' THEN
      blockers:=blockers||'"already_at_target"'::jsonb;
    ELSIF observation->'current_value'<>step #> '{parameters,current_value}' THEN
      blockers:=blockers||'"configuration_current_value_mismatch"'::jsonb;
    END IF;
  ELSIF action='restore_dependency' THEN
    IF (jsonb_typeof(observation->'available')='boolean'
      AND observation->'current_value'='null'::jsonb
      AND observation->'release_enabled'='null'::jsonb) IS NOT TRUE THEN
      RAISE EXCEPTION '候选检查依赖观测必须为唯一布尔参数';
    END IF;
    IF observation->'available'=step #> '{parameters,target_available}' THEN
      blockers:=blockers||'"already_at_target"'::jsonb;
    ELSIF observation->'available'<>step #> '{parameters,expected_available}' THEN
      blockers:=blockers||'"dependency_current_state_mismatch"'::jsonb;
    END IF;
  ELSE
    IF (jsonb_typeof(observation->'release_enabled')='boolean'
      AND observation->'current_value'='null'::jsonb
      AND observation->'available'='null'::jsonb) IS NOT TRUE THEN
      RAISE EXCEPTION '候选检查版本观测只能记录故障开关';
    END IF;
  END IF;
  expected_content:=jsonb_build_object('version','candidate-execution-preflight-content-v1',
    'decision_id',decision.id::text,'decision_content_sha256',decision.content_sha256,
    'plan_content_sha256',plan.content_sha256,'step',step,'observation',observation,
    'preconditions',plan.content->'preconditions',
    'unresolved_questions',plan.content->'unresolved_questions','blockers',blockers,
    'status','blocked','execution_eligible',FALSE);
  IF preflight.content IS DISTINCT FROM expected_content
    OR preflight.content_sha256 IS DISTINCT FROM
    encode(sha256(convert_to(candidate_canonical_json(preflight.content),'UTF8')),'hex') THEN
    RAISE EXCEPTION '候选检查内容必须精确保留计划条件、观测及确定性阻断';
  END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION guard_candidate_execution_preflight() RETURNS trigger AS $$
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选执行前检查不可更新或删除'; END IF;
  PERFORM assert_candidate_execution_preflight(NEW);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_execution_preflights_guard BEFORE INSERT OR UPDATE OR DELETE
  ON candidate_execution_preflights FOR EACH ROW
  EXECUTE FUNCTION guard_candidate_execution_preflight();
"""

_STATE = """
CREATE FUNCTION validate_candidate_preflight_state() RETURNS trigger AS $$
DECLARE preflight candidate_execution_preflights%ROWTYPE;
  audit candidate_preflight_commands%ROWTYPE; preflight_tx XID; audit_tx XID;
BEGIN
  IF TG_TABLE_NAME='candidate_execution_preflights' THEN
    SELECT * INTO preflight FROM candidate_execution_preflights WHERE id=NEW.id;
  ELSIF NEW.outcome='applied' THEN
    SELECT * INTO preflight FROM candidate_execution_preflights WHERE id=NEW.preflight_id;
  ELSE RETURN NULL;
  END IF;
  SELECT * INTO audit FROM candidate_preflight_commands
    WHERE authorization_id=preflight.authorization_id;
  IF (preflight.id IS NOT NULL AND audit.outcome='applied' AND audit.preflight_id=preflight.id
    AND audit.created_at=preflight.created_at AND audit.subject_id=preflight.requested_by_id
    AND audit.investigation_run_id=preflight.investigation_run_id
    AND audit.candidate_job_id=preflight.candidate_job_id
    AND audit.command_sha256=preflight.command_sha256) IS NOT TRUE THEN
    RAISE EXCEPTION '候选检查缺少同事务精确成功命令闭环';
  END IF;
  SELECT xmin INTO preflight_tx FROM candidate_execution_preflights WHERE id=preflight.id;
  SELECT xmin INTO audit_tx FROM candidate_preflight_commands WHERE cursor=audit.cursor;
  IF preflight_tx IS DISTINCT FROM audit_tx THEN
    RAISE EXCEPTION '候选检查与成功命令必须同事务精确绑定';
  END IF;
  PERFORM assert_candidate_execution_preflight(preflight);
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER candidate_execution_preflights_state
  AFTER INSERT ON candidate_execution_preflights
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_candidate_preflight_state();
CREATE CONSTRAINT TRIGGER candidate_preflight_commands_state
  AFTER INSERT ON candidate_preflight_commands
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_candidate_preflight_state();
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM candidate_execution_preflights)
          OR EXISTS (SELECT 1 FROM candidate_preflight_commands) THEN
          RAISE EXCEPTION '不能删除已保存的候选检查事实或命令审计';
        END IF;
      END $$;
      DROP TRIGGER candidate_execution_preflights_state ON candidate_execution_preflights;
      DROP TRIGGER candidate_preflight_commands_state ON candidate_preflight_commands;
      DROP FUNCTION validate_candidate_preflight_state();
      DROP TRIGGER candidate_execution_preflights_guard ON candidate_execution_preflights;
      DROP FUNCTION guard_candidate_execution_preflight();
      DROP FUNCTION assert_candidate_execution_preflight(candidate_execution_preflights);
      DROP TRIGGER candidate_preflight_commands_guard ON candidate_preflight_commands;
      DROP FUNCTION guard_candidate_preflight_command();
      DROP FUNCTION normalize_candidate_preflight_command(TEXT);
      DROP TABLE candidate_preflight_commands;
      DROP TABLE candidate_execution_preflights;
    """)
