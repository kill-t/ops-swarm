"""候选发布执行准入、只读观测与不可变请求闭环。"""

from alembic import op

revision = "0080_candidate_release_request"
down_revision = "0079_lab_release_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(_COMMANDS)
    op.execute(_REQUESTS)
    op.execute(_STATE)


_TABLES = """

CREATE TABLE candidate_release_execution_requests (
  id UUID NOT NULL,
  investigation_run_id UUID NOT NULL,
  candidate_job_id UUID NOT NULL,
  approval_decision_id UUID NOT NULL,
  approval_content_sha256 VARCHAR(64) NOT NULL,
  verification_version INTEGER NOT NULL,
  operation_id UUID NOT NULL,
  incident_version INTEGER NOT NULL,
  status VARCHAR(16) NOT NULL,
  policy_version VARCHAR(40) NOT NULL,
  requested_by_id VARCHAR(100) NOT NULL,
  authorization_id UUID NOT NULL,
  idempotency_key VARCHAR(100) NOT NULL,
  command_sha256 VARCHAR(64) NOT NULL,
  content JSONB NOT NULL,
  content_sha256 VARCHAR(64) NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
  PRIMARY KEY (id),
  CONSTRAINT fk_candidate_release_request_job_run FOREIGN KEY(candidate_job_id,
  investigation_run_id) REFERENCES investigation_candidate_evaluation_jobs (id,
  investigation_run_id) ON DELETE RESTRICT,
  CONSTRAINT uq_candidate_release_request_approval UNIQUE (approval_decision_id),
  CONSTRAINT uq_candidate_release_request_operation UNIQUE (operation_id),
  CONSTRAINT uq_candidate_release_request_authorization UNIQUE (authorization_id),
  CONSTRAINT uq_candidate_release_request_key UNIQUE (investigation_run_id,
  requested_by_id, idempotency_key),
  CONSTRAINT ck_candidate_release_request_versions CHECK (verification_version BETWEEN 1
  AND 8 AND incident_version>=2),
  CONSTRAINT ck_candidate_release_request_policy CHECK (status='prepared' AND
  policy_version='candidate-release-request-v1'),
  CONSTRAINT ck_candidate_release_request_expiry CHECK (expires_at>created_at),
  CONSTRAINT ck_candidate_release_request_hashes CHECK (approval_content_sha256 ~
  '^[0-9a-f]{64}$' AND command_sha256 ~ '^[0-9a-f]{64}$' AND content_sha256 ~
  '^[0-9a-f]{64}$'),
  CONSTRAINT ck_candidate_release_request_content CHECK (jsonb_typeof(content)='object'
  AND octet_length(content::text)<=65536),
  CONSTRAINT ck_candidate_release_request_subject CHECK
  (char_length(btrim(requested_by_id)) BETWEEN 1 AND 100),
  CONSTRAINT ck_candidate_release_request_key CHECK (char_length(idempotency_key) BETWEEN
  8 AND 100 AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'),
  FOREIGN KEY(approval_decision_id) REFERENCES condition_approval_decisions (id) ON DELETE
  RESTRICT,
  FOREIGN KEY(authorization_id) REFERENCES operator_authorizations (id) ON DELETE RESTRICT
)

;

CREATE TABLE candidate_release_execution_commands (
  cursor BIGINT GENERATED ALWAYS AS IDENTITY,
  authorization_id UUID NOT NULL,
  investigation_run_id UUID NOT NULL,
  candidate_job_id UUID NOT NULL,
  verification_version INTEGER NOT NULL,
  subject_id VARCHAR(100) NOT NULL,
  command JSONB NOT NULL,
  raw_body TEXT NOT NULL,
  command_sha256 VARCHAR(64) NOT NULL,
  outcome VARCHAR(16) NOT NULL,
  http_status INTEGER NOT NULL,
  request_id UUID,
  error_code VARCHAR(100),
  error_message VARCHAR(1000),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  PRIMARY KEY (cursor),
  CONSTRAINT uq_candidate_release_command_authorization UNIQUE (authorization_id),
  CONSTRAINT ck_candidate_release_command_version CHECK (verification_version BETWEEN 1
  AND 8),
  CONSTRAINT ck_candidate_release_command_body CHECK (jsonb_typeof(command)='object' AND
  octet_length(command::text)<=32768 AND octet_length(raw_body) BETWEEN 2 AND 65536),
  CONSTRAINT ck_candidate_release_command_hash CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_candidate_release_command_subject CHECK (char_length(btrim(subject_id))
  BETWEEN 1 AND 100),
  CONSTRAINT ck_candidate_release_command_outcome CHECK ((outcome IN
  ('applied','replayed') AND request_id IS NOT NULL AND error_code IS NULL AND
  error_message IS NULL AND ((outcome='applied' AND http_status=201) OR
  (outcome='replayed' AND http_status=200))) OR (outcome='error' AND request_id IS NULL
  AND error_code IS NOT NULL AND error_message IS NOT NULL AND http_status IN (404,409))),
  FOREIGN KEY(authorization_id) REFERENCES operator_authorizations (id) ON DELETE RESTRICT,
  FOREIGN KEY(request_id) REFERENCES candidate_release_execution_requests (id) ON DELETE
  RESTRICT
)

;
CREATE INDEX ix_candidate_release_commands_scope_cursor ON
  candidate_release_execution_commands (investigation_run_id, candidate_job_id,
  verification_version, cursor);
"""

_COMMANDS = """
CREATE FUNCTION normalize_candidate_release_command(raw_body TEXT) RETURNS JSONB AS $$
DECLARE body JSONB; fields TEXT[]; identifier TEXT; result JSONB;
BEGIN
 IF octet_length(convert_to(raw_body,'UTF8')) NOT BETWEEN 2 AND 65536 THEN RETURN NULL;
  END IF;
 body:=raw_body::jsonb;
 fields:=ARRAY['approval_decision_id','approval_content_sha256','expected_incident_version',
   'idempotency_key','reason'];
 IF (jsonb_typeof(body)='object' AND body ?& fields AND body-fields='{}'
   AND jsonb_typeof(body->'approval_decision_id')='string'
   AND jsonb_typeof(body->'approval_content_sha256')='string'
   AND candidate_strip_text(body->>'approval_content_sha256') ~ '^[0-9a-f]{64}$'
   AND jsonb_typeof(body->'expected_incident_version')='number'
   AND raw_body::json->>'expected_incident_version' ~ '^[0-9]+$'
   AND (body->>'expected_incident_version')::numeric>=2
   AND jsonb_typeof(body->'idempotency_key')='string'
   AND char_length(candidate_strip_text(body->>'idempotency_key')) BETWEEN 8 AND 100
   AND candidate_strip_text(body->>'idempotency_key') ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'
   AND jsonb_typeof(body->'reason')='string'
   AND char_length(candidate_strip_text(body->>'reason')) BETWEEN 1 AND 500) IS NOT TRUE THEN
   RETURN NULL; END IF;
 identifier:=candidate_condition_uuid(body->>'approval_decision_id');
 IF identifier IS NULL THEN RETURN NULL; END IF;
 result:=jsonb_build_object('approval_decision_id',identifier,
   'approval_content_sha256',candidate_strip_text(body->>'approval_content_sha256'),
   'expected_incident_version',body->'expected_incident_version',
   'idempotency_key',candidate_strip_text(body->>'idempotency_key'),
   'reason',candidate_strip_text(body->>'reason'));
 IF octet_length(convert_to(candidate_canonical_json(result),'UTF8'))>32768 THEN RETURN NULL;
 END IF;
 RETURN result;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

CREATE FUNCTION assert_candidate_release_admission(
 admission_id UUID, subject TEXT, run_id UUID, job_id UUID, version INTEGER,
 created TIMESTAMPTZ, lock_admission BOOLEAN) RETURNS VOID AS $$
DECLARE admission operator_authorizations%ROWTYPE;
 route TEXT:='/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}'
   ||'/execution-conditions/versions/{version}/release-execution-request';
BEGIN
 IF lock_admission THEN
   SELECT * INTO admission FROM operator_authorizations WHERE id=admission_id FOR UPDATE;
 ELSE SELECT * INTO admission FROM operator_authorizations WHERE id=admission_id;
 END IF;
 IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
   AND admission.subject_id=subject AND admission.role='executor'
   AND admission.permissions ? 'execute_action' AND admission.method='POST'
   AND admission.operation='prepare_candidate_release_execution' AND admission.route=route
   AND admission.resources=jsonb_build_object('run_id',run_id::text,'job_id',job_id::text,
     'version',version::text) AND admission.authorized_at<=created) IS NOT TRUE THEN
   RAISE EXCEPTION '候选发布请求缺少精确原始正文或同源可信准入'; END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION guard_candidate_release_command() RETURNS trigger AS $$
DECLARE admission operator_authorizations%ROWTYPE;
 request candidate_release_execution_requests%ROWTYPE;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选发布请求命令不可更新或删除'; END IF;
 PERFORM assert_candidate_release_admission(NEW.authorization_id,NEW.subject_id,
   NEW.investigation_run_id,NEW.candidate_job_id,NEW.verification_version,NEW.created_at,TRUE);
 SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
 IF admission.body_sha256 IS DISTINCT FROM
  encode(sha256(convert_to(NEW.raw_body,'UTF8')),'hex')
   THEN RAISE EXCEPTION '候选发布请求缺少精确原始正文或同源可信准入'; END IF;
 IF normalize_candidate_release_command(NEW.raw_body) IS DISTINCT FROM NEW.command THEN
   RAISE EXCEPTION '候选发布请求原始正文与规范命令不一致'; END IF;
 IF NEW.command_sha256 IS DISTINCT FROM encode(sha256(convert_to(
   candidate_canonical_json(NEW.command),'UTF8')),'hex') THEN
   RAISE EXCEPTION '候选发布请求命令摘要不一致'; END IF;
 IF NEW.outcome='error' THEN
   IF EXISTS (SELECT 1 FROM candidate_release_execution_requests r
     WHERE r.investigation_run_id=NEW.investigation_run_id
       AND r.candidate_job_id=NEW.candidate_job_id
       AND r.verification_version=NEW.verification_version AND
  r.requested_by_id=NEW.subject_id
       AND r.idempotency_key=NEW.command->>'idempotency_key'
       AND r.command_sha256=NEW.command_sha256) THEN
     RAISE EXCEPTION '已有候选发布请求不能降级为业务拒绝审计'; END IF;
   -- 换键可记录正常业务冲突，但不能掩盖已存在目标的历史损坏。
   SELECT * INTO request FROM candidate_release_execution_requests
     WHERE investigation_run_id=NEW.investigation_run_id AND
  candidate_job_id=NEW.candidate_job_id
       AND verification_version=NEW.verification_version;
   IF request.id IS NOT NULL THEN
     PERFORM assert_candidate_release_request(request,TRUE,FALSE);
   END IF;
   RETURN NEW;
 END IF;
 SELECT * INTO request FROM candidate_release_execution_requests WHERE id=NEW.request_id;
 IF (request.id IS NOT NULL AND request.investigation_run_id=NEW.investigation_run_id
   AND request.candidate_job_id=NEW.candidate_job_id
   AND request.verification_version=NEW.verification_version
   AND request.requested_by_id=NEW.subject_id AND request.command_sha256=NEW.command_sha256
   AND request.approval_decision_id::text=NEW.command->>'approval_decision_id'
   AND request.approval_content_sha256=NEW.command->>'approval_content_sha256'
   AND to_jsonb(request.incident_version)=NEW.command->'expected_incident_version'
   AND request.idempotency_key=NEW.command->>'idempotency_key'
   AND request.created_at<=NEW.created_at
   AND ((NEW.outcome='applied' AND request.authorization_id=NEW.authorization_id
     AND request.created_at=NEW.created_at)
     OR (NEW.outcome='replayed' AND request.authorization_id<>NEW.authorization_id))) IS
  NOT TRUE THEN
   RAISE EXCEPTION '候选发布请求审计缺少同源成功事实'; END IF;
 IF NEW.outcome='replayed' THEN PERFORM assert_candidate_release_request(request,TRUE,FALSE);
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_release_command_guard BEFORE INSERT OR UPDATE OR DELETE
 ON candidate_release_execution_commands FOR EACH ROW
 EXECUTE FUNCTION guard_candidate_release_command();
"""

_REQUESTS = """
CREATE FUNCTION candidate_release_timestamp(value JSONB) RETURNS TIMESTAMPTZ AS $$
DECLARE parsed TIMESTAMPTZ;
BEGIN
 IF (jsonb_typeof(value)='string' AND value#>>'{}' ~
   ('^[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]'
    ||'([.][0-9]{1,6})?(Z|[+-](0[0-9]|1[0-5]):[0-5][0-9])$')) IS NOT TRUE THEN RETURN NULL;
 END IF;
 parsed:=(value#>>'{}')::timestamptz;
 IF extract(year FROM parsed AT TIME ZONE 'UTC') NOT BETWEEN 1 AND 9999 THEN RETURN NULL;
  END IF;
 RETURN parsed;
EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

CREATE FUNCTION assert_candidate_release_request(
 request candidate_release_execution_requests, require_audit BOOLEAN, require_current BOOLEAN)
RETURNS VOID AS $$
DECLARE approval condition_approval_decisions%ROWTYPE;
 verification candidate_condition_verifications%ROWTYPE; plan candidate_action_plans%ROWTYPE;
 admission operator_authorizations%ROWTYPE; audit
  candidate_release_execution_commands%ROWTYPE;
 observation JSONB; state JSONB; observed TIMESTAMPTZ; state_updated TIMESTAMPTZ;
 step JSONB; expected JSONB; expected_cas JSONB;
BEGIN
 SELECT * INTO approval FROM condition_approval_decisions WHERE
  id=request.approval_decision_id;
 PERFORM assert_condition_approval_decision(approval,TRUE,FALSE);
 SELECT * INTO verification FROM candidate_condition_verifications WHERE
  id=approval.verification_id;
 SELECT * INTO plan FROM candidate_action_plans WHERE id=(approval.content->>'plan_id')::uuid;
 PERFORM assert_candidate_release_admission(request.authorization_id,request.requested_by_id,
   request.investigation_run_id,request.candidate_job_id,request.verification_version,
   request.created_at,FALSE);
 IF (approval.id IS NOT NULL AND approval.outcome='approved'
   AND approval.investigation_run_id=request.investigation_run_id
   AND approval.candidate_job_id=request.candidate_job_id
   AND approval.content_sha256=request.approval_content_sha256
   AND approval.verification_version=request.verification_version
   AND approval.incident_version=request.incident_version
   AND approval.created_at<=request.created_at AND request.created_at<request.expires_at
   AND request.created_at<verification.expires_at) IS NOT TRUE THEN
   RAISE EXCEPTION '候选发布请求缺少精确核验批准或原有效期限'; END IF;
 IF (plan.id IS NOT NULL AND jsonb_array_length(plan.content->'plan'->'steps')=1)
   IS NOT TRUE THEN RAISE EXCEPTION '候选发布请求必须绑定原单步骤计划'; END IF;
 step:=plan.content->'plan'->'steps'->0;
 IF (step->>'action_type'='rollback_release' AND step->>'target_type'='service'
   AND step->>'target_name'='order-service'
   AND step->'parameters'=jsonb_build_object('current_version','2026.01.01-rc1',
     'target_version','stable')) IS NOT TRUE THEN
   RAISE EXCEPTION '候选发布请求动作或参数不在白名单'; END IF;
 observation:=request.content->'observation'; state:=observation->'state';
 observed:=candidate_release_timestamp(observation->'observed_at');
 state_updated:=candidate_release_timestamp(state->'updated_at');
 IF (jsonb_typeof(observation)='object'
   AND observation ?& ARRAY['state','observed_at','source_path']
   AND observation-ARRAY['state','observed_at','source_path']='{}'
   AND observation->>'source_path'='/__admin/simulated-release'
   AND jsonb_typeof(state)='object'
   AND state ?& ARRAY['service_name','source','simulation','version','revision','command_count',
     'release_500_enabled','updated_at']
   AND state-ARRAY['service_name','source','simulation','version','revision','command_count',
     'release_500_enabled','updated_at']='{}'
   AND state->>'service_name'='order-service'
   AND state->>'source'='local-release-simulation-v1'
   AND state->'simulation'='true'::jsonb AND state->'release_500_enabled'='true'::jsonb
   AND state->>'version'='2026.01.01-rc1'
   AND jsonb_typeof(state->'revision')='number' AND state->>'revision' ~ '^[1-9][0-9]*$'
   AND (state->>'revision')::numeric BETWEEN 1 AND 2147483647
   AND jsonb_typeof(state->'command_count')='number' AND state->>'command_count' ~ '^[0-9]+$'
   AND (state->>'command_count')::numeric BETWEEN 0 AND 2147483647
   AND (state->>'revision')::numeric<=(state->>'command_count')::numeric+1
   AND observed IS NOT NULL AND state_updated IS NOT NULL AND state_updated<=observed
   AND approval.created_at<=observed AND observed<=request.created_at
   AND request.created_at<observed+interval '30 seconds'
   AND request.expires_at=least(verification.expires_at,observed+interval '30 seconds'))
   IS NOT TRUE THEN RAISE EXCEPTION '候选发布观测、完整修订或时效不合法'; END IF;
 expected_cas:=jsonb_build_object('expected_version','2026.01.01-rc1',
   'expected_revision',state->'revision','target_version','stable',
   'idempotency_key','candidate-release-'||request.operation_id::text);
 expected:=jsonb_build_object('version','candidate-release-execution-request-content-v1',
   'approval_decision_id',approval.id::text,'approval_content_sha256',approval.content_sha256,
   'verification_id',approval.verification_id::text,'verification_version',approval.verification_version,
   'verification_content_sha256',approval.verification_content_sha256,
   'plan_id',plan.id::text,'plan_content_sha256',plan.content_sha256,
   'operation_id',request.operation_id::text,'step',step,'observation',observation,
   'cas_command',expected_cas,'dispatch_eligible',FALSE);
 IF request.content IS DISTINCT FROM expected OR request.content_sha256 IS DISTINCT FROM
   encode(sha256(convert_to(candidate_canonical_json(request.content),'UTF8')),'hex') THEN
   RAISE EXCEPTION '候选发布请求内容或摘要无法重建'; END IF;
 IF require_current THEN PERFORM
  assert_condition_approval_current(verification,request.created_at);
 END IF;
 IF require_audit THEN
   SELECT * INTO admission FROM operator_authorizations WHERE id=request.authorization_id;
   SELECT * INTO audit FROM candidate_release_execution_commands
     WHERE authorization_id=request.authorization_id;
   IF (audit.outcome='applied' AND audit.http_status=201 AND audit.request_id=request.id
     AND audit.investigation_run_id=request.investigation_run_id
     AND audit.candidate_job_id=request.candidate_job_id
     AND audit.verification_version=request.verification_version
     AND audit.subject_id=request.requested_by_id AND audit.created_at=request.created_at
     AND audit.command_sha256=request.command_sha256
     AND normalize_candidate_release_command(audit.raw_body)=audit.command
     AND audit.command_sha256=encode(sha256(convert_to(
       candidate_canonical_json(audit.command),'UTF8')),'hex')
     AND admission.body_sha256=encode(sha256(convert_to(audit.raw_body,'UTF8')),'hex')
     AND audit.command->>'approval_decision_id'=request.approval_decision_id::text
     AND audit.command->>'approval_content_sha256'=request.approval_content_sha256
     AND audit.command->'expected_incident_version'=to_jsonb(request.incident_version)
     AND audit.command->>'idempotency_key'=request.idempotency_key) IS NOT TRUE THEN
     RAISE EXCEPTION '候选发布请求缺少精确成功命令历史'; END IF;
 END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION guard_candidate_release_request() RETURNS trigger AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选发布请求不可更新或删除'; END IF;
 PERFORM assert_candidate_release_admission(NEW.authorization_id,NEW.requested_by_id,
   NEW.investigation_run_id,NEW.candidate_job_id,NEW.verification_version,NEW.created_at,TRUE);
 PERFORM assert_candidate_release_request(NEW,FALSE,TRUE);
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_release_request_guard BEFORE INSERT OR UPDATE OR DELETE
 ON candidate_release_execution_requests FOR EACH ROW
 EXECUTE FUNCTION guard_candidate_release_request();
"""

_STATE = """
CREATE FUNCTION validate_candidate_release_request_state() RETURNS trigger AS $$
DECLARE request candidate_release_execution_requests%ROWTYPE;
 audit candidate_release_execution_commands%ROWTYPE; fact_tx XID; audit_tx XID;
BEGIN
 IF TG_TABLE_NAME='candidate_release_execution_requests' THEN
   SELECT * INTO request FROM candidate_release_execution_requests WHERE id=NEW.id;
 ELSIF NEW.outcome='applied' THEN
   SELECT * INTO request FROM candidate_release_execution_requests WHERE id=NEW.request_id;
 ELSE RETURN NULL;
 END IF;
 SELECT * INTO audit FROM candidate_release_execution_commands
   WHERE authorization_id=request.authorization_id;
 IF (request.id IS NOT NULL AND audit.outcome='applied' AND audit.request_id=request.id
   AND audit.created_at=request.created_at) IS NOT TRUE THEN
   RAISE EXCEPTION '候选发布请求缺少同事务精确成功命令闭环'; END IF;
 PERFORM assert_candidate_release_request(request,TRUE,TRUE);
 SELECT xmin INTO fact_tx FROM candidate_release_execution_requests WHERE id=request.id;
 SELECT xmin INTO audit_tx FROM candidate_release_execution_commands WHERE
  cursor=audit.cursor;
 IF fact_tx IS DISTINCT FROM audit_tx THEN
   RAISE EXCEPTION '候选发布请求与成功审计必须在同一事务保存'; END IF;
 RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER candidate_release_request_state
 AFTER INSERT ON candidate_release_execution_requests DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION validate_candidate_release_request_state();
CREATE CONSTRAINT TRIGGER candidate_release_command_state
 AFTER INSERT ON candidate_release_execution_commands DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION validate_candidate_release_request_state();
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM candidate_release_execution_requests)
          OR EXISTS (SELECT 1 FROM candidate_release_execution_commands) THEN
          RAISE EXCEPTION '不能删除已保存的候选发布请求或命令审计'; END IF;
      END $$;
      DROP TRIGGER candidate_release_request_state ON candidate_release_execution_requests;
      DROP TRIGGER candidate_release_command_state ON candidate_release_execution_commands;
      DROP FUNCTION validate_candidate_release_request_state();
      DROP TRIGGER candidate_release_request_guard ON candidate_release_execution_requests;
      DROP FUNCTION guard_candidate_release_request();
      DROP TRIGGER candidate_release_command_guard ON candidate_release_execution_commands;
      DROP FUNCTION guard_candidate_release_command();
      DROP FUNCTION
  assert_candidate_release_request(candidate_release_execution_requests,BOOLEAN,BOOLEAN);
      DROP FUNCTION candidate_release_timestamp(JSONB);
      DROP FUNCTION
  assert_candidate_release_admission(UUID,TEXT,UUID,UUID,INTEGER,TIMESTAMPTZ,BOOLEAN);
      DROP FUNCTION normalize_candidate_release_command(TEXT);
      DROP TABLE candidate_release_execution_commands;
      DROP TABLE candidate_release_execution_requests;
    """)
