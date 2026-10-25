"""精确核验版本的独立可信审批与不可变双阶段审计。"""

from alembic import op

revision = "0078_condition_approval"
down_revision = "0077_candidate_conditions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(_COMMANDS)
    op.execute(_FACTS)
    op.execute(_STATE)


_TABLES = """

CREATE TABLE condition_approval_requests (
	id UUID NOT NULL,
	investigation_run_id UUID NOT NULL,
	candidate_job_id UUID NOT NULL,
	verification_id UUID NOT NULL,
	verification_content_sha256 VARCHAR(64) NOT NULL,
	verification_version INTEGER NOT NULL,
	incident_version INTEGER NOT NULL,
	policy_version VARCHAR(40) NOT NULL,
	status VARCHAR(16) NOT NULL,
	requested_by_id VARCHAR(100) NOT NULL,
	authorization_id UUID NOT NULL,
	idempotency_key VARCHAR(100) NOT NULL,
	command_sha256 VARCHAR(64) NOT NULL,
	content JSONB NOT NULL,
	content_sha256 VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT fk_condition_approval_request_job_run FOREIGN KEY(candidate_job_id,
   investigation_run_id) REFERENCES investigation_candidate_evaluation_jobs (id,
   investigation_run_id) ON DELETE RESTRICT,
	CONSTRAINT uq_condition_approval_request_authorization UNIQUE (authorization_id),
	CONSTRAINT uq_condition_approval_request_key UNIQUE (investigation_run_id, requested_by_id,
   idempotency_key),
	CONSTRAINT ck_condition_approval_request_policy CHECK (policy_version='condition-approval-v1'),
	CONSTRAINT ck_condition_approval_request_version CHECK (incident_version>=2 AND
   verification_version BETWEEN 1 AND 8),
	CONSTRAINT ck_condition_approval_request_hashes CHECK (verification_content_sha256 ~
   '^[0-9a-f]{64}$' AND command_sha256 ~ '^[0-9a-f]{64}$' AND content_sha256 ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_condition_approval_request_content CHECK (jsonb_typeof(content)='object' AND
   octet_length(content::text)<=65536),
	CONSTRAINT ck_condition_approval_request_subject CHECK (char_length(btrim(requested_by_id))
   BETWEEN 1 AND 100),
	CONSTRAINT ck_condition_approval_request_key CHECK (char_length(idempotency_key) BETWEEN 8 AND
   100 AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'),
	CONSTRAINT uq_condition_approval_request_verification UNIQUE (verification_id),
	CONSTRAINT ck_condition_approval_request_status CHECK (status='pending'),
	CONSTRAINT ck_condition_approval_request_expiry CHECK (expires_at>created_at),
	FOREIGN KEY(verification_id) REFERENCES candidate_condition_verifications (id)
   ON DELETE RESTRICT,
	FOREIGN KEY(authorization_id) REFERENCES operator_authorizations (id) ON DELETE RESTRICT
)

;

CREATE TABLE condition_approval_decisions (
	id UUID NOT NULL,
	investigation_run_id UUID NOT NULL,
	candidate_job_id UUID NOT NULL,
	request_id UUID NOT NULL,
	request_content_sha256 VARCHAR(64) NOT NULL,
	verification_id UUID NOT NULL,
	verification_content_sha256 VARCHAR(64) NOT NULL,
	verification_version INTEGER NOT NULL,
	incident_version INTEGER NOT NULL,
	policy_version VARCHAR(40) NOT NULL,
	outcome VARCHAR(16) NOT NULL,
	decided_by_id VARCHAR(100) NOT NULL,
	authorization_id UUID NOT NULL,
	idempotency_key VARCHAR(100) NOT NULL,
	command_sha256 VARCHAR(64) NOT NULL,
	content JSONB NOT NULL,
	content_sha256 VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT fk_condition_approval_decision_job_run FOREIGN KEY(candidate_job_id,
   investigation_run_id) REFERENCES investigation_candidate_evaluation_jobs (id,
   investigation_run_id) ON DELETE RESTRICT,
	CONSTRAINT uq_condition_approval_decision_authorization UNIQUE (authorization_id),
	CONSTRAINT uq_condition_approval_decision_key UNIQUE (investigation_run_id, decided_by_id,
   idempotency_key),
	CONSTRAINT ck_condition_approval_decision_policy CHECK (policy_version='condition-approval-v1'),
	CONSTRAINT ck_condition_approval_decision_version CHECK (incident_version>=2 AND
   verification_version BETWEEN 1 AND 8),
	CONSTRAINT ck_condition_approval_decision_hashes CHECK (verification_content_sha256 ~
   '^[0-9a-f]{64}$' AND command_sha256 ~ '^[0-9a-f]{64}$' AND content_sha256 ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_condition_approval_decision_content CHECK (jsonb_typeof(content)='object' AND
   octet_length(content::text)<=65536),
	CONSTRAINT ck_condition_approval_decision_subject CHECK (char_length(btrim(decided_by_id))
   BETWEEN 1 AND 100),
	CONSTRAINT ck_condition_approval_decision_key CHECK (char_length(idempotency_key) BETWEEN 8 AND
   100 AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'),
	CONSTRAINT uq_condition_approval_decision_request UNIQUE (request_id),
	CONSTRAINT ck_condition_approval_decision_request CHECK (request_content_sha256 ~
   '^[0-9a-f]{64}$'),
	CONSTRAINT ck_condition_approval_decision_outcome CHECK (outcome IN
   ('approved','rejected','expired')),
	FOREIGN KEY(request_id) REFERENCES condition_approval_requests (id) ON DELETE RESTRICT,
	FOREIGN KEY(verification_id) REFERENCES candidate_condition_verifications (id)
   ON DELETE RESTRICT,
	FOREIGN KEY(authorization_id) REFERENCES operator_authorizations (id) ON DELETE RESTRICT
)

;

CREATE TABLE condition_approval_commands (
	cursor BIGINT GENERATED ALWAYS AS IDENTITY,
	authorization_id UUID NOT NULL,
	investigation_run_id UUID NOT NULL,
	candidate_job_id UUID NOT NULL,
	verification_version INTEGER NOT NULL,
	stage VARCHAR(16) NOT NULL,
	subject_id VARCHAR(100) NOT NULL,
	command JSONB NOT NULL,
	raw_body TEXT NOT NULL,
	command_sha256 VARCHAR(64) NOT NULL,
	outcome VARCHAR(16) NOT NULL,
	http_status INTEGER NOT NULL,
	request_id UUID,
	decision_id UUID,
	error_code VARCHAR(100),
	error_message VARCHAR(1000),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (cursor),
	CONSTRAINT uq_condition_approval_commands_authorization UNIQUE (authorization_id),
	CONSTRAINT ck_condition_approval_commands_body CHECK (jsonb_typeof(command)='object' AND
   octet_length(convert_to(candidate_canonical_json(command),'UTF8'))<=32768),
	CONSTRAINT ck_condition_approval_commands_raw CHECK (
   octet_length(raw_body) BETWEEN 2 AND 65536),
	CONSTRAINT ck_condition_approval_commands_stage CHECK (stage IN ('request','decision')),
	CONSTRAINT ck_condition_approval_commands_version CHECK (verification_version BETWEEN 1 AND 8),
	CONSTRAINT ck_condition_approval_commands_hash CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_condition_approval_commands_subject CHECK (char_length(btrim(subject_id)) BETWEEN
   1 AND 100),
	CONSTRAINT ck_condition_approval_commands_outcome CHECK ((outcome IN ('applied','replayed') AND
   request_id IS NOT NULL AND ((stage='request' AND decision_id IS NULL) OR (stage='decision'
   AND decision_id IS NOT NULL)) AND error_code IS NULL AND error_message IS NULL AND
   ((outcome='applied' AND http_status=201) OR (outcome='replayed' AND http_status=200))) OR
   (outcome='error' AND request_id IS NULL AND decision_id IS NULL AND error_code IS NOT NULL
   AND error_message IS NOT NULL AND http_status IN (404,409))),
	CONSTRAINT ck_condition_approval_commands_error CHECK (error_code IS NULL OR error_code ~
   '^[a-z0-9_]{1,100}$'),
	FOREIGN KEY(authorization_id) REFERENCES operator_authorizations (id) ON DELETE RESTRICT,
	FOREIGN KEY(request_id) REFERENCES condition_approval_requests (id) ON DELETE RESTRICT,
	FOREIGN KEY(decision_id) REFERENCES condition_approval_decisions (id) ON DELETE RESTRICT
)

;
CREATE INDEX ix_condition_approval_commands_run_job_version_cursor ON
   condition_approval_commands (investigation_run_id, candidate_job_id, verification_version,
   cursor);
"""

_COMMANDS = """
CREATE FUNCTION normalize_condition_approval_command(stage TEXT, raw_body TEXT)
RETURNS JSONB AS $$
DECLARE body JSONB; fields TEXT[]; target TEXT; result JSONB; acknowledgement JSONB;
BEGIN
 IF octet_length(convert_to(raw_body,'UTF8'))>65536 THEN RETURN NULL; END IF;
 body:=raw_body::jsonb;
 IF stage='request' THEN
   fields:=ARRAY['verification_id','verification_content_sha256','expected_incident_version',
     'idempotency_key','reason'];
 ELSE
   IF stage<>'decision' THEN RETURN NULL; END IF;
   fields:=ARRAY['action','request_id','request_content_sha256','expected_incident_version',
     'idempotency_key','reason'];
 END IF;
 IF (jsonb_typeof(body)='object' AND body ?& fields
   AND (body-fields-CASE WHEN stage='decision' THEN ARRAY['acknowledge_verification']
     ELSE ARRAY[]::text[] END)='{}'::jsonb
   AND jsonb_typeof(body->'expected_incident_version')='number'
   AND raw_body::json->>'expected_incident_version' ~ '^[0-9]+$'
   AND (body->>'expected_incident_version')::numeric>=2
   AND jsonb_typeof(body->'idempotency_key')='string'
   AND char_length(candidate_strip_text(body->>'idempotency_key')) BETWEEN 8 AND 100
   AND candidate_strip_text(body->>'idempotency_key') ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'
   AND jsonb_typeof(body->'reason')='string'
   AND char_length(candidate_strip_text(body->>'reason')) BETWEEN 1 AND 500)
   IS NOT TRUE THEN RETURN NULL; END IF;
 IF stage='request' THEN
   IF (jsonb_typeof(body->'verification_id')='string'
     AND jsonb_typeof(body->'verification_content_sha256')='string'
     AND candidate_strip_text(body->>'verification_content_sha256') ~ '^[0-9a-f]{64}$')
     IS NOT TRUE THEN RETURN NULL; END IF;
   target:=candidate_condition_uuid(body->>'verification_id');
   IF target IS NULL THEN RETURN NULL; END IF;
   result:=jsonb_build_object('verification_id',target,
     'verification_content_sha256',candidate_strip_text(body->>'verification_content_sha256'));
 ELSE
   acknowledgement:=COALESCE(body->'acknowledge_verification','false'::jsonb);
   IF (jsonb_typeof(body->'action')='string' AND body->>'action' IN ('approve','reject','expire')
     AND jsonb_typeof(body->'request_id')='string'
     AND jsonb_typeof(body->'request_content_sha256')='string'
     AND candidate_strip_text(body->>'request_content_sha256') ~ '^[0-9a-f]{64}$'
     AND jsonb_typeof(acknowledgement)='boolean'
     AND acknowledgement=to_jsonb(body->>'action'='approve')) IS NOT TRUE THEN RETURN NULL;
   END IF;
   target:=candidate_condition_uuid(body->>'request_id');
   IF target IS NULL THEN RETURN NULL; END IF;
   result:=jsonb_build_object('action',body->>'action','request_id',target,
     'request_content_sha256',candidate_strip_text(body->>'request_content_sha256'),
     'acknowledge_verification',acknowledgement);
 END IF;
 result:=result||jsonb_build_object('expected_incident_version',body->'expected_incident_version',
   'idempotency_key',candidate_strip_text(body->>'idempotency_key'),
   'reason',candidate_strip_text(body->>'reason'));
 IF octet_length(convert_to(candidate_canonical_json(result),'UTF8'))>32768 THEN RETURN NULL;
 END IF;
 RETURN result;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

CREATE FUNCTION assert_condition_approval_admission(
 admission_id UUID, subject TEXT, stage TEXT, run_id UUID, job_id UUID,
 version INTEGER, created TIMESTAMPTZ, lock_admission BOOLEAN)
RETURNS VOID AS $$
DECLARE admission operator_authorizations%ROWTYPE;
 route TEXT:='/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}'
   ||'/execution-conditions/versions/{version}/approval-request';
BEGIN
 IF lock_admission THEN
   SELECT * INTO admission FROM operator_authorizations WHERE id=admission_id FOR UPDATE;
 ELSE
   SELECT * INTO admission FROM operator_authorizations WHERE id=admission_id;
 END IF;
 IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
   AND admission.subject_id=subject AND admission.method='POST'
   AND ((stage='request' AND admission.role='engineer'
     AND admission.permissions ? 'request_approval'
     AND admission.operation='create_condition_approval_request' AND admission.route=route)
   OR (stage='decision' AND admission.role='approver'
     AND admission.permissions ? 'decide_approval'
     AND admission.operation='decide_condition_approval' AND admission.route=route||'/decision'))
   AND admission.resources=jsonb_build_object('run_id',run_id::text,'job_id',job_id::text,
     'version',version::text)
   AND admission.authorized_at<=created) IS NOT TRUE THEN
   RAISE EXCEPTION '核验审批缺少精确原始正文或同源可信准入';
 END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION guard_condition_approval_command() RETURNS trigger AS $$
DECLARE admission operator_authorizations%ROWTYPE;
 request condition_approval_requests%ROWTYPE; decision condition_approval_decisions%ROWTYPE;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '核验审批命令审计不可更新或删除'; END IF;
 PERFORM assert_condition_approval_admission(NEW.authorization_id,NEW.subject_id,NEW.stage,
   NEW.investigation_run_id,NEW.candidate_job_id,NEW.verification_version,NEW.created_at,TRUE);
 SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
 IF admission.body_sha256 IS DISTINCT FROM encode(sha256(convert_to(NEW.raw_body,'UTF8')),'hex')
   THEN RAISE EXCEPTION '核验审批缺少精确原始正文或同源可信准入'; END IF;
 IF normalize_condition_approval_command(NEW.stage,NEW.raw_body) IS DISTINCT FROM NEW.command THEN
   RAISE EXCEPTION '核验审批原始正文与规范命令不一致'; END IF;
 IF NEW.command_sha256 IS DISTINCT FROM encode(sha256(convert_to(
   candidate_canonical_json(NEW.command),'UTF8')),'hex') THEN
   RAISE EXCEPTION '核验审批命令缺少精确规范摘要'; END IF;
 IF NEW.outcome='error' THEN
   IF (NEW.stage='request' AND EXISTS (SELECT 1 FROM condition_approval_requests r
     WHERE r.investigation_run_id=NEW.investigation_run_id
       AND r.candidate_job_id=NEW.candidate_job_id
       AND r.verification_version=NEW.verification_version AND r.requested_by_id=NEW.subject_id
       AND r.idempotency_key=NEW.command->>'idempotency_key'
       AND r.command_sha256=NEW.command_sha256))
     OR (NEW.stage='decision' AND EXISTS (SELECT 1 FROM condition_approval_decisions d
     WHERE d.investigation_run_id=NEW.investigation_run_id
       AND d.candidate_job_id=NEW.candidate_job_id
       AND d.verification_version=NEW.verification_version AND d.decided_by_id=NEW.subject_id
       AND d.idempotency_key=NEW.command->>'idempotency_key'
       AND d.command_sha256=NEW.command_sha256)) THEN
     RAISE EXCEPTION '已有核验审批事实不能降级为业务拒绝审计'; END IF;
   RETURN NEW;
 END IF;
 SELECT * INTO request FROM condition_approval_requests WHERE id=NEW.request_id;
 IF NEW.stage='request' THEN
   IF (request.id IS NOT NULL AND request.investigation_run_id=NEW.investigation_run_id
     AND request.candidate_job_id=NEW.candidate_job_id
     AND request.verification_version=NEW.verification_version
     AND request.requested_by_id=NEW.subject_id AND request.command_sha256=NEW.command_sha256
     AND request.verification_id::text=NEW.command->>'verification_id'
     AND request.verification_content_sha256=NEW.command->>'verification_content_sha256'
     AND to_jsonb(request.incident_version)=NEW.command->'expected_incident_version'
     AND request.idempotency_key=NEW.command->>'idempotency_key'
     AND request.created_at<=NEW.created_at
     AND ((NEW.outcome='applied' AND request.authorization_id=NEW.authorization_id
       AND request.created_at=NEW.created_at)
       OR (NEW.outcome='replayed' AND request.authorization_id<>NEW.authorization_id)))
     IS NOT TRUE THEN RAISE EXCEPTION '核验审批命令结果与请求事实不一致'; END IF;
   IF NEW.outcome='replayed' THEN
     PERFORM assert_condition_approval_request(request,TRUE,FALSE);
   END IF;
 ELSE
   SELECT * INTO decision FROM condition_approval_decisions WHERE id=NEW.decision_id;
   IF (decision.id IS NOT NULL AND decision.investigation_run_id=NEW.investigation_run_id
     AND decision.candidate_job_id=NEW.candidate_job_id
     AND decision.verification_version=NEW.verification_version
     AND decision.decided_by_id=NEW.subject_id AND decision.command_sha256=NEW.command_sha256
     AND decision.request_id=NEW.request_id
     AND decision.request_id::text=NEW.command->>'request_id'
     AND decision.request_content_sha256=NEW.command->>'request_content_sha256'
     AND to_jsonb(decision.incident_version)=NEW.command->'expected_incident_version'
     AND decision.idempotency_key=NEW.command->>'idempotency_key'
     AND decision.outcome=CASE NEW.command->>'action' WHEN 'approve' THEN 'approved'
       WHEN 'reject' THEN 'rejected' WHEN 'expire' THEN 'expired' END
     AND decision.created_at<=NEW.created_at
     AND ((NEW.outcome='applied' AND decision.authorization_id=NEW.authorization_id
       AND decision.created_at=NEW.created_at)
       OR (NEW.outcome='replayed' AND decision.authorization_id<>NEW.authorization_id)))
     IS NOT TRUE THEN RAISE EXCEPTION '核验审批命令结果与决定事实不一致'; END IF;
   IF NEW.outcome='replayed' THEN
     PERFORM assert_condition_approval_decision(decision,TRUE,FALSE);
   END IF;
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER condition_approval_commands_guard BEFORE INSERT OR UPDATE OR DELETE
 ON condition_approval_commands FOR EACH ROW EXECUTE FUNCTION guard_condition_approval_command();
"""

_FACTS = """
CREATE FUNCTION assert_condition_approval_current(
 verification candidate_condition_verifications, created TIMESTAMPTZ)
RETURNS VOID AS $$
DECLARE decision candidate_approval_decisions%ROWTYPE; request candidate_approval_requests%ROWTYPE;
 job investigation_candidate_evaluation_jobs%ROWTYPE; run investigation_runs%ROWTYPE;
 incident incidents%ROWTYPE; current_version INTEGER; latest_version INTEGER;
BEGIN
 SELECT * INTO decision FROM candidate_approval_decisions WHERE id=verification.decision_id;
 SELECT * INTO request FROM candidate_approval_requests WHERE id=decision.request_id;
 SELECT * INTO job FROM investigation_candidate_evaluation_jobs WHERE
   id=verification.candidate_job_id;
 SELECT * INTO run FROM investigation_runs WHERE id=verification.investigation_run_id;
 SELECT * INTO incident FROM incidents WHERE id=run.incident_id;
 SELECT max(version) INTO latest_version FROM candidate_condition_verifications
   WHERE decision_id=verification.decision_id;
 IF (verification.version=latest_version
   AND verification.content->>'status'='ready_for_review'
   AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(verification.content->'items') item
     WHERE item->>'status'<>'verified')
   AND incident.status='mitigating' AND incident.version=verification.incident_version
   AND created<verification.expires_at AND created<request.expires_at
   AND created<run.started_at+
     make_interval(secs=>(run.call_budget->>'max_duration_seconds')::int)) IS NOT TRUE THEN
   RAISE EXCEPTION '核验审批需要最新完整核验、当前事故版本及原有效期限'; END IF;
 PERFORM assert_candidate_decision_request_history(request,TRUE);
 SELECT max(state_version) INTO current_version FROM investigation_human_wait_events
   WHERE investigation_run_id=run.id;
 IF (job.source_kind='terminal_handoff' AND run.status='await_candidate') IS NOT TRUE
   AND (job.source_kind='human_route' AND run.status='await_human'
     AND current_version IS NOT DISTINCT FROM request.wait_version
     AND NOT EXISTS (SELECT 1 FROM investigation_human_wait_events h
       WHERE h.investigation_run_id=run.id AND h.action='cancel')) IS NOT TRUE THEN
   RAISE EXCEPTION '核验审批原输入已补充、取消或不再适用'; END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION assert_condition_approval_request(
 request condition_approval_requests, require_audit BOOLEAN, require_current BOOLEAN)
RETURNS VOID AS $$
DECLARE verification candidate_condition_verifications%ROWTYPE; expected JSONB;
 audit condition_approval_commands%ROWTYPE; admission operator_authorizations%ROWTYPE;
BEGIN
 SELECT * INTO verification FROM candidate_condition_verifications WHERE id=request.verification_id;
 -- 复用完整历史前缀；不能用待追加模式，亦不能反锁原核验授权。
 PERFORM assert_candidate_condition_verification(verification,FALSE,FALSE);
 PERFORM assert_condition_approval_admission(request.authorization_id,request.requested_by_id,
   'request',request.investigation_run_id,request.candidate_job_id,
   request.verification_version,request.created_at,FALSE);
 IF (verification.id IS NOT NULL AND verification.investigation_run_id=request.investigation_run_id
   AND verification.candidate_job_id=request.candidate_job_id
   AND verification.content_sha256=request.verification_content_sha256
   AND verification.version=request.verification_version
   AND verification.incident_version=request.incident_version
   AND verification.created_at<=request.created_at AND request.created_at<request.expires_at
   AND request.expires_at=verification.expires_at
   AND verification.content->>'status'='ready_for_review'
   AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(verification.content->'items') item
     WHERE item->>'status'<>'verified')) IS NOT TRUE THEN
   RAISE EXCEPTION '核验审批请求缺少精确完整核验或原期限'; END IF;
 expected:=jsonb_build_object('version','condition-approval-request-content-v1',
   'verification_id',verification.id::text,'verification_version',verification.version,
   'verification_content_sha256',verification.content_sha256,
   'base_decision_id',verification.decision_id::text,
   'base_decision_content_sha256',verification.decision_content_sha256,
   'plan_id',verification.content->'plan_id',
   'plan_content_sha256',verification.content->'plan_content_sha256',
   'conditions_status','ready_for_review','execution_eligible',FALSE);
 IF request.content IS DISTINCT FROM expected OR request.content_sha256 IS DISTINCT FROM
   encode(sha256(convert_to(candidate_canonical_json(request.content),'UTF8')),'hex') THEN
   RAISE EXCEPTION '核验审批请求内容或摘要不完整'; END IF;
 IF require_current THEN PERFORM assert_condition_approval_current(verification,request.created_at);
 END IF;
 IF require_audit THEN
   SELECT * INTO admission FROM operator_authorizations WHERE id=request.authorization_id;
   SELECT * INTO audit FROM condition_approval_commands WHERE
   authorization_id=request.authorization_id;
   IF (audit.stage='request' AND audit.outcome='applied' AND audit.http_status=201
     AND audit.request_id=request.id AND audit.decision_id IS NULL
     AND audit.investigation_run_id=request.investigation_run_id
     AND audit.candidate_job_id=request.candidate_job_id
     AND audit.verification_version=request.verification_version
     AND audit.subject_id=request.requested_by_id AND audit.created_at=request.created_at
     AND audit.command_sha256=request.command_sha256
     AND normalize_condition_approval_command('request',audit.raw_body)=audit.command
     AND audit.command_sha256=encode(sha256(convert_to(
       candidate_canonical_json(audit.command),'UTF8')),'hex')
     AND admission.body_sha256=encode(sha256(convert_to(audit.raw_body,'UTF8')),'hex')
     AND audit.command->>'verification_id'=request.verification_id::text
     AND audit.command->>'verification_content_sha256'=request.verification_content_sha256
     AND audit.command->'expected_incident_version'=to_jsonb(request.incident_version)
     AND audit.command->>'idempotency_key'=request.idempotency_key) IS NOT TRUE THEN
     RAISE EXCEPTION '核验审批请求缺少精确成功命令历史'; END IF;
 END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION assert_condition_approval_decision(
 decision condition_approval_decisions, require_audit BOOLEAN, require_current BOOLEAN)
RETURNS VOID AS $$
DECLARE request condition_approval_requests%ROWTYPE; expected JSONB;
 verification candidate_condition_verifications%ROWTYPE;
 audit condition_approval_commands%ROWTYPE; admission operator_authorizations%ROWTYPE;
BEGIN
 SELECT * INTO request FROM condition_approval_requests WHERE id=decision.request_id;
 PERFORM assert_condition_approval_request(request,TRUE,FALSE);
 PERFORM assert_condition_approval_admission(decision.authorization_id,decision.decided_by_id,
   'decision',decision.investigation_run_id,decision.candidate_job_id,
   decision.verification_version,decision.created_at,FALSE);
 IF (request.id IS NOT NULL AND request.investigation_run_id=decision.investigation_run_id
   AND request.candidate_job_id=decision.candidate_job_id
   AND request.content_sha256=decision.request_content_sha256
   AND request.verification_id=decision.verification_id
   AND request.verification_content_sha256=decision.verification_content_sha256
   AND request.verification_version=decision.verification_version
   AND request.incident_version=decision.incident_version AND
   request.created_at<=decision.created_at
   AND ((decision.outcome='expired' AND decision.created_at>=request.expires_at)
     OR (decision.outcome IN ('approved','rejected') AND decision.created_at<request.expires_at)))
   IS NOT TRUE THEN RAISE EXCEPTION '核验审批决定缺少精确请求或动作期限'; END IF;
 expected:=request.content||jsonb_build_object('version','condition-approval-decision-content-v1',
   'request_id',request.id::text,'request_content_sha256',request.content_sha256,
   'outcome',decision.outcome);
 IF decision.content IS DISTINCT FROM expected OR decision.content_sha256 IS DISTINCT FROM
   encode(sha256(convert_to(candidate_canonical_json(decision.content),'UTF8')),'hex') THEN
   RAISE EXCEPTION '核验审批决定内容或摘要不完整'; END IF;
 IF require_current AND decision.outcome='approved' THEN
   SELECT * INTO verification FROM candidate_condition_verifications WHERE
   id=decision.verification_id;
   PERFORM assert_condition_approval_current(verification,decision.created_at);
 END IF;
 IF require_audit THEN
   SELECT * INTO admission FROM operator_authorizations WHERE id=decision.authorization_id;
   SELECT * INTO audit FROM condition_approval_commands WHERE
   authorization_id=decision.authorization_id;
   IF (audit.stage='decision' AND audit.outcome='applied' AND audit.http_status=201
     AND audit.request_id=request.id AND audit.decision_id=decision.id
     AND audit.investigation_run_id=decision.investigation_run_id
     AND audit.candidate_job_id=decision.candidate_job_id
     AND audit.verification_version=decision.verification_version
     AND audit.subject_id=decision.decided_by_id AND audit.created_at=decision.created_at
     AND audit.command_sha256=decision.command_sha256
     AND normalize_condition_approval_command('decision',audit.raw_body)=audit.command
     AND audit.command_sha256=encode(sha256(convert_to(
       candidate_canonical_json(audit.command),'UTF8')),'hex')
     AND admission.body_sha256=encode(sha256(convert_to(audit.raw_body,'UTF8')),'hex')
     AND audit.command->>'request_id'=request.id::text
     AND audit.command->>'request_content_sha256'=request.content_sha256
     AND audit.command->'expected_incident_version'=to_jsonb(decision.incident_version)
     AND audit.command->>'idempotency_key'=decision.idempotency_key
     AND decision.outcome=CASE audit.command->>'action' WHEN 'approve' THEN 'approved'
       WHEN 'reject' THEN 'rejected' WHEN 'expire' THEN 'expired' END) IS NOT TRUE THEN
     RAISE EXCEPTION '核验审批决定缺少精确成功命令历史'; END IF;
 END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION guard_condition_approval_fact() RETURNS trigger AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '核验审批事实不可更新或删除'; END IF;
 IF TG_TABLE_NAME='condition_approval_requests' THEN
   PERFORM assert_condition_approval_admission(NEW.authorization_id,NEW.requested_by_id,
     'request',NEW.investigation_run_id,NEW.candidate_job_id,
     NEW.verification_version,NEW.created_at,TRUE);
   PERFORM assert_condition_approval_request(NEW,FALSE,TRUE);
 ELSE
   PERFORM assert_condition_approval_admission(NEW.authorization_id,NEW.decided_by_id,
     'decision',NEW.investigation_run_id,NEW.candidate_job_id,
     NEW.verification_version,NEW.created_at,TRUE);
   PERFORM assert_condition_approval_decision(NEW,FALSE,TRUE);
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER condition_approval_requests_guard BEFORE INSERT OR UPDATE OR DELETE
 ON condition_approval_requests FOR EACH ROW EXECUTE FUNCTION guard_condition_approval_fact();
CREATE TRIGGER condition_approval_decisions_guard BEFORE INSERT OR UPDATE OR DELETE
 ON condition_approval_decisions FOR EACH ROW EXECUTE FUNCTION guard_condition_approval_fact();
"""

_STATE = """
CREATE FUNCTION validate_condition_approval_state() RETURNS trigger AS $$
DECLARE request condition_approval_requests%ROWTYPE;
 decision condition_approval_decisions%ROWTYPE; audit condition_approval_commands%ROWTYPE;
 fact_tx XID; audit_tx XID;
BEGIN
 IF TG_TABLE_NAME='condition_approval_requests' THEN
   SELECT * INTO request FROM condition_approval_requests WHERE id=NEW.id;
 ELSIF TG_TABLE_NAME='condition_approval_decisions' THEN
   SELECT * INTO decision FROM condition_approval_decisions WHERE id=NEW.id;
 ELSIF NEW.outcome='applied' THEN
   IF NEW.stage='request' THEN
     SELECT * INTO request FROM condition_approval_requests WHERE id=NEW.request_id;
   ELSE
     SELECT * INTO decision FROM condition_approval_decisions WHERE id=NEW.decision_id;
   END IF;
 ELSE RETURN NULL;
 END IF;
 IF request.id IS NOT NULL THEN
   SELECT * INTO audit FROM condition_approval_commands WHERE
   authorization_id=request.authorization_id;
   IF (audit.stage='request' AND audit.outcome='applied' AND audit.request_id=request.id
     AND audit.created_at=request.created_at) IS NOT TRUE THEN
     RAISE EXCEPTION '核验审批请求缺少同事务精确成功命令闭环'; END IF;
   SELECT xmin INTO fact_tx FROM condition_approval_requests WHERE id=request.id;
   PERFORM assert_condition_approval_request(request,TRUE,TRUE);
 ELSIF decision.id IS NOT NULL THEN
   SELECT * INTO audit FROM condition_approval_commands WHERE
   authorization_id=decision.authorization_id;
   IF (audit.stage='decision' AND audit.outcome='applied' AND audit.decision_id=decision.id
     AND audit.created_at=decision.created_at) IS NOT TRUE THEN
     RAISE EXCEPTION '核验审批决定缺少同事务精确成功命令闭环'; END IF;
   SELECT xmin INTO fact_tx FROM condition_approval_decisions WHERE id=decision.id;
   PERFORM assert_condition_approval_decision(decision,TRUE,TRUE);
 ELSE RAISE EXCEPTION '核验审批成功审计缺少业务事实';
 END IF;
 SELECT xmin INTO audit_tx FROM condition_approval_commands WHERE cursor=audit.cursor;
 IF fact_tx IS DISTINCT FROM audit_tx THEN
   RAISE EXCEPTION '核验审批事实与成功命令必须同事务精确绑定'; END IF;
 RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER condition_approval_requests_state AFTER INSERT ON
   condition_approval_requests
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_condition_approval_state();
CREATE CONSTRAINT TRIGGER condition_approval_decisions_state AFTER INSERT ON
   condition_approval_decisions
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_condition_approval_state();
CREATE CONSTRAINT TRIGGER condition_approval_commands_state AFTER INSERT ON
   condition_approval_commands
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_condition_approval_state();
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM condition_approval_requests)
          OR EXISTS (SELECT 1 FROM condition_approval_decisions)
          OR EXISTS (SELECT 1 FROM condition_approval_commands) THEN
          RAISE EXCEPTION '不能删除已保存的核验审批事实或命令审计';
        END IF;
      END $$;
      DROP TRIGGER condition_approval_commands_state ON condition_approval_commands;
      DROP TRIGGER condition_approval_decisions_state ON condition_approval_decisions;
      DROP TRIGGER condition_approval_requests_state ON condition_approval_requests;
      DROP FUNCTION validate_condition_approval_state();
      DROP TRIGGER condition_approval_requests_guard ON condition_approval_requests;
      DROP TRIGGER condition_approval_decisions_guard ON condition_approval_decisions;
      DROP FUNCTION guard_condition_approval_fact();
      DROP TRIGGER condition_approval_commands_guard ON condition_approval_commands;
      DROP FUNCTION guard_condition_approval_command();
      DROP FUNCTION assert_condition_approval_decision(
        condition_approval_decisions,BOOLEAN,BOOLEAN);
      DROP FUNCTION assert_condition_approval_request(condition_approval_requests,BOOLEAN,BOOLEAN);
      DROP FUNCTION assert_condition_approval_current(
        candidate_condition_verifications,TIMESTAMPTZ);
      DROP FUNCTION assert_condition_approval_admission(
        UUID,TEXT,TEXT,UUID,UUID,INTEGER,TIMESTAMPTZ,BOOLEAN);
      DROP FUNCTION normalize_condition_approval_command(TEXT,TEXT);
      DROP TABLE condition_approval_commands;
      DROP TABLE condition_approval_decisions;
      DROP TABLE condition_approval_requests;
    """)
