"""候选发布持久任务、可信命令、租约及不可变结果闭环。"""

from alembic import op

revision = "0082_candidate_release_job"
down_revision = "0081_lab_release_dispatch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(_COMMANDS)
    op.execute(_JOBS)
    op.execute(_RESULTS)
    op.execute(_STATE)


_TABLES = """
CREATE TABLE candidate_release_execution_jobs (
  id UUID NOT NULL,
  investigation_run_id UUID NOT NULL,
  candidate_job_id UUID NOT NULL,
  verification_version INTEGER NOT NULL,
  request_id UUID NOT NULL,
  request_content_sha256 VARCHAR(64) NOT NULL,
  operation_id UUID NOT NULL,
  policy_version VARCHAR(40) NOT NULL,
  requested_by_id VARCHAR(100) NOT NULL,
  credential_id VARCHAR(64) NOT NULL,
  credential_binding_sha256 VARCHAR(64) NOT NULL,
  credential_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
  authorization_id UUID NOT NULL,
  idempotency_key VARCHAR(100) NOT NULL,
  command_sha256 VARCHAR(64) NOT NULL,
  dispatch_command JSONB NOT NULL,
  dispatch_sha256 VARCHAR(64) NOT NULL,
  status VARCHAR(16) NOT NULL,
  phase VARCHAR(16) NOT NULL,
  attempt_count INTEGER NOT NULL,
  locked_by VARCHAR(100),
  lease_token UUID,
  locked_until TIMESTAMP WITH TIME ZONE,
  dispatched_at TIMESTAMP WITH TIME ZONE,
  dispatch_attempt_id UUID,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
  completed_at TIMESTAMP WITH TIME ZONE,
  last_error_code VARCHAR(100),
  PRIMARY KEY (id),
  CONSTRAINT uq_release_job_request UNIQUE (request_id),
  CONSTRAINT uq_release_job_operation UNIQUE (operation_id),
  CONSTRAINT uq_release_job_authorization UNIQUE (authorization_id),
  CONSTRAINT uq_release_job_key UNIQUE (investigation_run_id, requested_by_id, idempotency_key),
  CONSTRAINT ck_release_job_policy CHECK (policy_version='candidate-release-execution-v1'),
  CONSTRAINT ck_release_job_counts CHECK (verification_version BETWEEN 1 AND 8 AND attempt_count
  BETWEEN 0 AND 3),
  CONSTRAINT ck_release_job_status CHECK (status IN
  ('queued','running','succeeded','failed','unknown')),
  CONSTRAINT ck_release_job_phase CHECK ((phase='prepared' AND dispatched_at IS NULL AND
  dispatch_attempt_id IS NULL) OR (phase='dispatched' AND dispatched_at IS NOT NULL AND
  dispatch_attempt_id IS NOT NULL)),
  CONSTRAINT ck_release_job_lease CHECK ((status='running' AND locked_by IS NOT NULL AND
  lease_token IS NOT NULL AND locked_until IS NOT NULL AND completed_at IS NULL) OR
  (status<>'running' AND locked_by IS NULL AND lease_token IS NULL AND locked_until IS NULL AND
  ((status='queued' AND completed_at IS NULL) OR (status IN ('succeeded','failed','unknown') AND
  completed_at IS NOT NULL)))),
  CONSTRAINT ck_release_job_dispatch CHECK (jsonb_typeof(dispatch_command)='object' AND
  octet_length(dispatch_command::text)<=8192),
  CONSTRAINT ck_release_job_hashes CHECK (request_content_sha256 ~ '^[0-9a-f]{64}$' AND
  credential_binding_sha256 ~ '^[0-9a-f]{64}$' AND command_sha256 ~ '^[0-9a-f]{64}$' AND
  dispatch_sha256 ~ '^[0-9a-f]{64}$'),
  FOREIGN KEY(investigation_run_id) REFERENCES investigation_runs (id) ON DELETE RESTRICT,
  FOREIGN KEY(candidate_job_id) REFERENCES investigation_candidate_evaluation_jobs (id) ON DELETE
  RESTRICT,
  FOREIGN KEY(request_id) REFERENCES candidate_release_execution_requests (id) ON DELETE RESTRICT,
  FOREIGN KEY(authorization_id) REFERENCES operator_authorizations (id) ON DELETE RESTRICT
);

CREATE TABLE candidate_release_execution_attempts (
  id UUID NOT NULL,
  job_id UUID NOT NULL,
  number INTEGER NOT NULL,
  lease_token UUID NOT NULL,
  worker_id VARCHAR(100) NOT NULL,
  status VARCHAR(16) NOT NULL,
  started_at TIMESTAMP WITH TIME ZONE NOT NULL,
  lease_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
  finished_at TIMESTAMP WITH TIME ZONE,
  error_code VARCHAR(100),
  PRIMARY KEY (id),
  CONSTRAINT uq_release_attempt_number UNIQUE (job_id, number),
  CONSTRAINT uq_release_attempt_token UNIQUE (lease_token),
  CONSTRAINT ck_release_attempt_lease CHECK (number BETWEEN 1 AND 3 AND
  started_at<lease_expires_at),
  CONSTRAINT ck_release_attempt_status CHECK ((status='running' AND finished_at IS NULL AND
  error_code IS NULL) OR (status IN ('succeeded','failed','lease_expired') AND finished_at IS NOT
  NULL AND finished_at>=started_at)),
  FOREIGN KEY(job_id) REFERENCES candidate_release_execution_jobs (id)
  ON DELETE RESTRICT
);

CREATE TABLE candidate_release_execution_results (
  job_id UUID NOT NULL,
  attempt_id UUID NOT NULL,
  content JSONB NOT NULL,
  content_sha256 VARCHAR(64) NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  PRIMARY KEY (job_id),
  CONSTRAINT uq_release_result_attempt UNIQUE (attempt_id),
  CONSTRAINT ck_release_result_content CHECK (jsonb_typeof(content)='object' AND
  octet_length(content::text)<=65536 AND content_sha256 ~ '^[0-9a-f]{64}$'),
  FOREIGN KEY(job_id) REFERENCES candidate_release_execution_jobs (id)
  ON DELETE RESTRICT,
  FOREIGN KEY(attempt_id) REFERENCES candidate_release_execution_attempts (id) ON DELETE RESTRICT
);

CREATE TABLE candidate_release_execution_job_commands (
  cursor INTEGER GENERATED BY DEFAULT AS IDENTITY,
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
  execution_job_id UUID,
  error_code VARCHAR(100),
  error_message VARCHAR(500),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  PRIMARY KEY (cursor),
  CONSTRAINT uq_release_job_command_authorization UNIQUE (authorization_id),
  CONSTRAINT ck_release_command_version CHECK (verification_version BETWEEN 1 AND 8),
  CONSTRAINT ck_release_command_outcome CHECK ((outcome='applied' AND http_status=201 AND
  execution_job_id IS NOT NULL AND error_code IS NULL AND error_message IS NULL) OR
  (outcome='replayed' AND http_status=200 AND execution_job_id IS NOT NULL AND error_code IS NULL
  AND error_message IS NULL) OR (outcome='error' AND http_status IN (404,409) AND execution_job_id
  IS NULL AND error_code IS NOT NULL AND error_message IS NOT NULL)),
  CONSTRAINT ck_release_command_body CHECK (octet_length(raw_body) BETWEEN 2 AND 65536 AND
  jsonb_typeof(command)='object' AND octet_length(command::text)<=32768 AND command_sha256 ~
  '^[0-9a-f]{64}$'),
  FOREIGN KEY(authorization_id) REFERENCES operator_authorizations (id) ON DELETE RESTRICT,
  FOREIGN KEY(execution_job_id) REFERENCES candidate_release_execution_jobs (id)
  ON DELETE RESTRICT
);
CREATE INDEX ix_release_job_claim ON candidate_release_execution_jobs (status, locked_until,
  created_at);
CREATE INDEX ix_release_job_commands_cursor ON candidate_release_execution_job_commands
  (investigation_run_id, candidate_job_id, verification_version, cursor);
ALTER TABLE candidate_release_execution_jobs ADD CONSTRAINT fk_release_job_dispatch_attempt
  FOREIGN KEY(dispatch_attempt_id) REFERENCES candidate_release_execution_attempts (id) ON DELETE
  RESTRICT;
"""


_COMMANDS = """
CREATE FUNCTION normalize_release_job_command(raw_body TEXT) RETURNS JSONB AS $$
DECLARE body JSONB; fields TEXT[]; identifier TEXT; result JSONB;
BEGIN
 IF octet_length(convert_to(raw_body,'UTF8')) NOT BETWEEN 2 AND 65536 THEN RETURN NULL;
  END IF;
 body:=raw_body::jsonb;
 fields:=ARRAY['request_id','request_content_sha256','expected_incident_version',
   'idempotency_key','reason'];
 IF (jsonb_typeof(body)='object' AND body ?& fields AND body-fields='{}'
   AND jsonb_typeof(body->'request_id')='string'
   AND jsonb_typeof(body->'request_content_sha256')='string'
   AND candidate_strip_text(body->>'request_content_sha256') ~ '^[0-9a-f]{64}$'
   AND jsonb_typeof(body->'expected_incident_version')='number'
   AND raw_body::json->>'expected_incident_version' ~ '^[0-9]+$'
   AND (body->>'expected_incident_version')::numeric>=2
   AND jsonb_typeof(body->'idempotency_key')='string'
   AND char_length(candidate_strip_text(body->>'idempotency_key')) BETWEEN 8 AND 100
   AND candidate_strip_text(body->>'idempotency_key') ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'
   AND jsonb_typeof(body->'reason')='string'
   AND char_length(candidate_strip_text(body->>'reason')) BETWEEN 1 AND 500) IS NOT TRUE THEN
   RETURN NULL; END IF;
 identifier:=candidate_condition_uuid(body->>'request_id');
 IF identifier IS NULL THEN RETURN NULL; END IF;
 result:=jsonb_build_object('request_id',identifier,
   'request_content_sha256',candidate_strip_text(body->>'request_content_sha256'),
   'expected_incident_version',body->'expected_incident_version',
   'idempotency_key',candidate_strip_text(body->>'idempotency_key'),
   'reason',candidate_strip_text(body->>'reason'));
 IF octet_length(convert_to(candidate_canonical_json(result),'UTF8'))>32768 THEN RETURN NULL;
 END IF;
 RETURN result;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

CREATE FUNCTION assert_release_job_admission(
 admission_id UUID, subject TEXT, run_id UUID, job_id UUID, version INTEGER,
 created TIMESTAMPTZ, lock_admission BOOLEAN) RETURNS VOID AS $$
DECLARE admission operator_authorizations%ROWTYPE;
 route TEXT:='/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}'
   ||'/execution-conditions/versions/{version}/release-execution-request/execution-job';
BEGIN
 IF lock_admission THEN
   SELECT * INTO admission FROM operator_authorizations WHERE id=admission_id FOR UPDATE;
 ELSE SELECT * INTO admission FROM operator_authorizations WHERE id=admission_id;
 END IF;
 IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
   AND admission.subject_id=subject AND admission.role='executor'
   AND admission.permissions ? 'execute_action' AND admission.method='POST'
   AND admission.operation='create_candidate_release_execution_job' AND admission.route=route
   AND admission.resources=jsonb_build_object('run_id',run_id::text,'job_id',job_id::text,
     'version',version::text) AND admission.authorized_at<=created) IS NOT TRUE THEN
   RAISE EXCEPTION '候选发布请求缺少精确原始正文或同源可信准入'; END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION guard_release_job_command() RETURNS trigger AS $$
DECLARE admission operator_authorizations%ROWTYPE; job candidate_release_execution_jobs%ROWTYPE;
 request candidate_release_execution_requests%ROWTYPE;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '发布执行命令不可更新或删除'; END IF;
 PERFORM assert_release_job_admission(NEW.authorization_id,NEW.subject_id,
   NEW.investigation_run_id,NEW.candidate_job_id,NEW.verification_version,NEW.created_at,TRUE);
 SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
 IF admission.body_sha256 IS DISTINCT FROM
   encode(sha256(convert_to(NEW.raw_body,'UTF8')),'hex') THEN
   RAISE EXCEPTION '发布执行命令缺少精确原始正文或同源可信准入'; END IF;
 IF normalize_release_job_command(NEW.raw_body) IS DISTINCT FROM NEW.command THEN
   RAISE EXCEPTION '发布执行原始正文与规范命令不一致'; END IF;
 IF NEW.command_sha256 IS DISTINCT FROM encode(sha256(convert_to(
   candidate_canonical_json(NEW.command),'UTF8')),'hex') THEN
   RAISE EXCEPTION '发布执行命令摘要不一致'; END IF;
 IF NEW.outcome='error' THEN
   IF EXISTS (SELECT 1 FROM candidate_release_execution_jobs j WHERE
     j.investigation_run_id=NEW.investigation_run_id AND j.requested_by_id=NEW.subject_id
     AND j.idempotency_key=NEW.command->>'idempotency_key'
     AND j.command_sha256=NEW.command_sha256) THEN
     RAISE EXCEPTION '已有发布执行任务不能降级为业务拒绝审计'; END IF;
   FOR job IN SELECT * FROM candidate_release_execution_jobs WHERE
     request_id=(NEW.command->>'request_id')::uuid OR
     (investigation_run_id=NEW.investigation_run_id AND candidate_job_id=NEW.candidate_job_id
       AND verification_version=NEW.verification_version) LOOP
     PERFORM assert_release_job_history(job);
   END LOOP;
   SELECT * INTO request FROM candidate_release_execution_requests
     WHERE id=(NEW.command->>'request_id')::uuid;
   IF request.id IS NOT NULL THEN
     PERFORM assert_candidate_release_request(request,TRUE,FALSE); END IF;
   RETURN NEW;
 END IF;
 SELECT * INTO job FROM candidate_release_execution_jobs WHERE id=NEW.execution_job_id;
 SELECT * INTO request FROM candidate_release_execution_requests WHERE id=job.request_id;
 IF (job.id IS NOT NULL AND job.investigation_run_id=NEW.investigation_run_id
   AND job.candidate_job_id=NEW.candidate_job_id AND
  job.verification_version=NEW.verification_version
   AND job.requested_by_id=NEW.subject_id AND job.command_sha256=NEW.command_sha256
   AND job.request_id::text=NEW.command->>'request_id'
   AND job.request_content_sha256=NEW.command->>'request_content_sha256'
   AND to_jsonb(request.incident_version)=NEW.command->'expected_incident_version'
   AND job.idempotency_key=NEW.command->>'idempotency_key' AND job.created_at<=NEW.created_at
   AND ((NEW.outcome='applied' AND job.authorization_id=NEW.authorization_id
     AND job.created_at=NEW.created_at)
     OR (NEW.outcome='replayed' AND job.authorization_id<>NEW.authorization_id))) IS NOT TRUE THEN
   RAISE EXCEPTION '发布执行命令缺少精确同源成功事实'; END IF;
 IF NEW.outcome='replayed' THEN PERFORM assert_release_job_history(job); END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER release_job_command_guard BEFORE INSERT OR UPDATE OR DELETE
 ON candidate_release_execution_job_commands FOR EACH ROW
 EXECUTE FUNCTION guard_release_job_command();
"""


_JOBS = """
CREATE FUNCTION assert_release_job(job candidate_release_execution_jobs,
 require_audit BOOLEAN, require_current BOOLEAN) RETURNS VOID AS $$
DECLARE request candidate_release_execution_requests%ROWTYPE;
 approval condition_approval_decisions%ROWTYPE; verification
  candidate_condition_verifications%ROWTYPE;
 admission operator_authorizations%ROWTYPE; audit candidate_release_execution_job_commands%ROWTYPE;
 command JSONB; expiry TIMESTAMPTZ;
BEGIN
 SELECT * INTO request FROM candidate_release_execution_requests WHERE id=job.request_id;
 PERFORM assert_candidate_release_request(request,TRUE,FALSE);
 PERFORM assert_release_job_admission(job.authorization_id,job.requested_by_id,
   job.investigation_run_id,job.candidate_job_id,job.verification_version,job.created_at,FALSE);
 SELECT * INTO admission FROM operator_authorizations WHERE id=job.authorization_id;
 expiry:=least(request.expires_at,job.credential_expires_at);
 command:=job.dispatch_command;
 IF (request.id IS NOT NULL AND request.investigation_run_id=job.investigation_run_id
   AND request.candidate_job_id=job.candidate_job_id
   AND request.verification_version=job.verification_version
   AND request.content_sha256=job.request_content_sha256 AND request.operation_id=job.operation_id
   AND request.created_at<=job.created_at AND job.created_at<expiry
   AND admission.credential_id=job.credential_id
   AND job.credential_binding_sha256 ~ '^[0-9a-f]{64}$'
   AND isfinite(job.created_at) AND isfinite(job.credential_expires_at)
   AND jsonb_typeof(command)='object'
   AND command ?& ARRAY['operation_id','command','not_before','expires_at']
   AND command-ARRAY['operation_id','command','not_before','expires_at']='{}'
   AND command->>'operation_id'=job.operation_id::text
   AND command->'command'=request.content->'cas_command'
   AND candidate_release_timestamp(command->'not_before')=job.created_at
   AND candidate_release_timestamp(command->'expires_at')=expiry
   AND expiry-job.created_at<=interval '30 seconds'
   AND job.dispatch_sha256=encode(sha256(convert_to(
     candidate_canonical_json(command),'UTF8')),'hex')) IS NOT TRUE THEN
   RAISE EXCEPTION '发布执行任务缺少原请求、身份、精确命令或固定窗口'; END IF;
 IF require_current THEN
   SELECT * INTO approval FROM condition_approval_decisions WHERE id=request.approval_decision_id;
   SELECT * INTO verification FROM candidate_condition_verifications WHERE
  id=approval.verification_id;
   PERFORM assert_condition_approval_current(verification,job.created_at);
 END IF;
 IF require_audit THEN
   SELECT * INTO audit FROM candidate_release_execution_job_commands
     WHERE authorization_id=job.authorization_id;
   IF (audit.outcome='applied' AND audit.http_status=201 AND audit.execution_job_id=job.id
     AND audit.investigation_run_id=job.investigation_run_id
     AND audit.candidate_job_id=job.candidate_job_id
     AND audit.verification_version=job.verification_version
     AND audit.subject_id=job.requested_by_id AND audit.created_at=job.created_at
     AND audit.command_sha256=job.command_sha256
     AND normalize_release_job_command(audit.raw_body)=audit.command
     AND admission.body_sha256=encode(sha256(convert_to(audit.raw_body,'UTF8')),'hex')
     AND audit.command_sha256=encode(sha256(convert_to(
       candidate_canonical_json(audit.command),'UTF8')),'hex')
     AND audit.command->>'request_id'=request.id::text
     AND audit.command->>'request_content_sha256'=request.content_sha256
     AND audit.command->'expected_incident_version'=to_jsonb(request.incident_version)
     AND audit.command->>'idempotency_key'=job.idempotency_key) IS NOT TRUE THEN
     RAISE EXCEPTION '发布执行任务缺少精确成功命令历史'; END IF;
 END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION guard_release_job() RETURNS trigger AS $$
DECLARE attempt candidate_release_execution_attempts%ROWTYPE;
 request candidate_release_execution_requests%ROWTYPE;
 approval condition_approval_decisions%ROWTYPE;
 verification candidate_condition_verifications%ROWTYPE;
 expiry TIMESTAMPTZ;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION '发布执行任务不可删除'; END IF;
 IF TG_OP='INSERT' THEN
   IF (NEW.status='queued' AND NEW.phase='prepared' AND NEW.attempt_count=0
     AND NEW.updated_at=NEW.created_at AND NEW.last_error_code IS NULL) IS NOT TRUE THEN
     RAISE EXCEPTION '发布执行任务只能从queued和prepared创建'; END IF;
   PERFORM assert_release_job_admission(NEW.authorization_id,NEW.requested_by_id,
     NEW.investigation_run_id,NEW.candidate_job_id,NEW.verification_version,NEW.created_at,TRUE);
   PERFORM assert_release_job(NEW,FALSE,TRUE);
 ELSE
   IF (to_jsonb(NEW)-ARRAY['status','phase','attempt_count','locked_by','lease_token',
     'locked_until','dispatched_at','dispatch_attempt_id','updated_at','completed_at','last_error_code'])
     IS DISTINCT FROM
     (to_jsonb(OLD)-ARRAY['status','phase','attempt_count','locked_by','lease_token',
     'locked_until','dispatched_at','dispatch_attempt_id','updated_at','completed_at','last_error_code'])
     OR OLD.status IN ('succeeded','failed','unknown') OR NEW.updated_at<OLD.updated_at
     OR NOT isfinite(NEW.updated_at) THEN
     RAISE EXCEPTION '发布执行原文、身份、窗口及终态不可改写'; END IF;
   IF NEW.phase IS DISTINCT FROM OLD.phase THEN
     expiry:=candidate_release_timestamp(NEW.dispatch_command->'expires_at');
     SELECT * INTO attempt FROM candidate_release_execution_attempts
       WHERE id=NEW.dispatch_attempt_id;
     IF (OLD.phase='prepared' AND NEW.phase='dispatched' AND OLD.status='running'
       AND NEW.status='running' AND OLD.lease_token=NEW.lease_token
       AND OLD.attempt_count=NEW.attempt_count AND OLD.locked_by=NEW.locked_by
       AND OLD.locked_until=NEW.locked_until AND attempt.job_id=NEW.id
       AND attempt.number=NEW.attempt_count AND attempt.status='running'
       AND attempt.lease_token=NEW.lease_token AND attempt.worker_id=NEW.locked_by
       AND NEW.dispatched_at=NEW.updated_at AND NEW.dispatched_at>=attempt.started_at
       AND NEW.dispatched_at<expiry AND expiry<=NEW.locked_until
       AND NEW.updated_at<OLD.locked_until AND clock_timestamp()<OLD.locked_until
       AND clock_timestamp()<expiry) IS NOT TRUE THEN
       RAISE EXCEPTION '发布派发需要有效原租约、尝试及固定窗口'; END IF;
     -- 创建准入不能替代首次写派发时的最新核验、原累计预算和当前事故检查。
     SELECT * INTO request FROM candidate_release_execution_requests WHERE id=NEW.request_id;
     PERFORM assert_candidate_release_request(request,TRUE,FALSE);
     SELECT * INTO approval FROM condition_approval_decisions WHERE id=request.approval_decision_id;
     SELECT * INTO verification FROM candidate_condition_verifications
       WHERE id=approval.verification_id;
     PERFORM assert_condition_approval_current(verification,NEW.dispatched_at);
   ELSIF NEW.dispatched_at IS DISTINCT FROM OLD.dispatched_at
     OR NEW.dispatch_attempt_id IS DISTINCT FROM OLD.dispatch_attempt_id THEN
     RAISE EXCEPTION '发布派发时刻与尝试只能固定一次'; END IF;
   IF NEW.status='running' AND NEW.lease_token IS DISTINCT FROM OLD.lease_token THEN
     IF ((OLD.status='queued' OR (OLD.status='running' AND OLD.locked_until<=NEW.updated_at))
       AND OLD.attempt_count<3 AND NEW.attempt_count=OLD.attempt_count+1
       AND NEW.locked_until=NEW.updated_at+interval '60 seconds'
       AND NEW.phase=OLD.phase AND NEW.last_error_code IS NULL) IS NOT TRUE THEN
       RAISE EXCEPTION '发布执行领取需要未占用或已过期任务及固定60秒租约'; END IF;
   ELSIF NEW.status='running' THEN
     IF (OLD.status='running' AND NEW.attempt_count=OLD.attempt_count
       AND NEW.lease_token=OLD.lease_token AND NEW.locked_by=OLD.locked_by
       AND NEW.locked_until=OLD.locked_until AND NEW.updated_at<OLD.locked_until
       AND NEW.phase<>OLD.phase) IS NOT TRUE THEN
       RAISE EXCEPTION '发布执行租约不可续期或原地改写'; END IF;
   ELSIF NEW.status IN ('succeeded','failed','unknown') THEN
     IF (OLD.status='running' AND NEW.attempt_count=OLD.attempt_count
       AND NEW.phase=OLD.phase AND NEW.completed_at=NEW.updated_at
       AND (NEW.updated_at<OLD.locked_until OR
         (OLD.attempt_count=3 AND OLD.locked_until<=NEW.updated_at
           AND NEW.status=CASE OLD.phase WHEN 'dispatched' THEN 'unknown' ELSE 'failed' END)))
       IS NOT TRUE THEN RAISE EXCEPTION '发布执行终态需要当前租约或第三次过期收敛'; END IF;
     IF NEW.updated_at<OLD.locked_until AND clock_timestamp()>=OLD.locked_until THEN
       RAISE EXCEPTION '迟到发布执行结果不能回填旧租约时间'; END IF;
   ELSE RAISE EXCEPTION '发布执行状态迁移不合法'; END IF;
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER release_job_guard BEFORE INSERT OR UPDATE OR DELETE
 ON candidate_release_execution_jobs FOR EACH ROW EXECUTE FUNCTION guard_release_job();

CREATE FUNCTION guard_release_attempt() RETURNS trigger AS $$
DECLARE job candidate_release_execution_jobs%ROWTYPE;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION '发布执行尝试不可删除'; END IF;
 SELECT * INTO job FROM candidate_release_execution_jobs WHERE id=NEW.job_id FOR UPDATE;
 IF TG_OP='INSERT' THEN
   IF (NEW.status='running' AND job.status='running' AND NEW.number=job.attempt_count
     AND NEW.lease_token=job.lease_token AND NEW.worker_id=job.locked_by
     AND NEW.started_at=job.updated_at AND NEW.lease_expires_at=job.locked_until
     AND NEW.lease_expires_at=NEW.started_at+interval '60 seconds') IS NOT TRUE THEN
     RAISE EXCEPTION '发布执行尝试必须绑定当前任务租约'; END IF;
 ELSE
   IF (to_jsonb(NEW)-ARRAY['status','finished_at','error_code']) IS DISTINCT FROM
     (to_jsonb(OLD)-ARRAY['status','finished_at','error_code']) OR OLD.status<>'running'
     OR NEW.status='running' THEN RAISE EXCEPTION '发布执行尝试原文及终态不可改写'; END IF;
   IF NEW.status='lease_expired' THEN
     IF NEW.finished_at<NEW.lease_expires_at OR NEW.error_code IS NULL THEN
       RAISE EXCEPTION '发布执行尝试尚未过期'; END IF;
   ELSIF (NEW.finished_at<NEW.lease_expires_at AND clock_timestamp()<NEW.lease_expires_at
     AND NEW.number=job.attempt_count
     AND (job.lease_token=NEW.lease_token OR job.status IN ('succeeded','failed','unknown')))
     IS NOT TRUE THEN RAISE EXCEPTION '迟到或旧发布执行尝试不可结算'; END IF;
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER release_attempt_guard BEFORE INSERT OR UPDATE OR DELETE
 ON candidate_release_execution_attempts FOR EACH ROW EXECUTE FUNCTION guard_release_attempt();
"""


_RESULTS = """
CREATE FUNCTION assert_release_result(result candidate_release_execution_results) RETURNS VOID AS $$
DECLARE job candidate_release_execution_jobs%ROWTYPE; attempt
  candidate_release_execution_attempts%ROWTYPE;
 body JSONB; observation JSONB; view JSONB; record JSONB; receipt JSONB; state JSONB; command JSONB;
 observed TIMESTAMPTZ; checked TIMESTAMPTZ; registered TIMESTAMPTZ; finished TIMESTAMPTZ;
 outcome TEXT; current_effect BOOLEAN; expected_status TEXT;
BEGIN
 SELECT * INTO job FROM candidate_release_execution_jobs WHERE id=result.job_id;
 SELECT * INTO attempt FROM candidate_release_execution_attempts WHERE id=result.attempt_id;
 PERFORM assert_release_job(job,TRUE,FALSE);
 body:=result.content; outcome:=body->>'outcome';
 IF (body ?& ARRAY['version','request_id','request_content_sha256','operation_id','dispatch_sha256',
   'outcome','observation','reason_code','created_at','parent_advanced']
   AND body-ARRAY['version','request_id','request_content_sha256','operation_id','dispatch_sha256',
     'outcome','observation','reason_code','created_at','parent_advanced']='{}'
   AND body->>'version'='candidate-release-execution-result-v1'
   AND body->>'request_id'=job.request_id::text
   AND body->>'request_content_sha256'=job.request_content_sha256
   AND body->>'operation_id'=job.operation_id::text AND body->>'dispatch_sha256'=job.dispatch_sha256
   AND body->'parent_advanced'='false'::jsonb
   AND jsonb_typeof(body->'reason_code')='string'
   AND char_length(candidate_strip_text(body->>'reason_code')) BETWEEN 1 AND 100
   AND candidate_release_timestamp(body->'created_at')=result.created_at
   AND result.content_sha256=encode(sha256(convert_to(candidate_canonical_json(body),'UTF8')),'hex')
   AND attempt.job_id=job.id AND attempt.number=job.attempt_count
   AND attempt.started_at<=result.created_at
   AND ((outcome='blocked' AND job.phase='prepared') OR
     (outcome IN ('applied_current','applied_superseded','not_applied','unknown')
       AND job.phase='dispatched'))) IS NOT TRUE THEN
   RAISE EXCEPTION '发布执行结果缺少精确任务、尝试、摘要或合法阶段'; END IF;
 observation:=body->'observation';
 IF result.created_at>=attempt.lease_expires_at THEN
   IF (attempt.number=3 AND attempt.status='lease_expired'
     AND observation='null'::jsonb
     AND outcome=CASE job.phase WHEN 'dispatched' THEN 'unknown' ELSE 'blocked' END)
     IS NOT TRUE THEN
     RAISE EXCEPTION '耗尽租约只能保存第三次尝试的无观测保守结果'; END IF;
 END IF;
 IF observation='null'::jsonb THEN
   IF outcome NOT IN ('unknown','blocked') THEN
     RAISE EXCEPTION '确定发布结果必须携带完整只读材料'; END IF;
   RETURN;
 END IF;
 view:=observation->'view'; record:=view->'record'; receipt:=view->'receipt';
 state:=receipt->'current'; command:=receipt->'command';
 observed:=candidate_release_timestamp(observation->'observed_at');
 checked:=candidate_release_timestamp(view->'checked_at');
 registered:=candidate_release_timestamp(record->'created_at');
 finished:=candidate_release_timestamp(record->'finished_at');
 IF (jsonb_typeof(observation)='object' AND observation ?& ARRAY['view','observed_at','source_path']
   AND observation-ARRAY['view','observed_at','source_path']='{}'
   AND observation->>'source_path' IN ('/__admin/simulated-release/dispatches',
     '/__admin/simulated-release/dispatches/'||job.operation_id::text)
   AND jsonb_typeof(view)='object' AND view ?& ARRAY['record','availability','checked_at','receipt']
   AND view-ARRAY['record','availability','checked_at','receipt']='{}'
   AND jsonb_typeof(record)='object'
   AND record ?& ARRAY['operation_id','command','not_before','expires_at','policy_version',
     'command_sha256','status','command_id','created_at','finished_at']
   AND record-ARRAY['operation_id','command','not_before','expires_at','policy_version',
     'command_sha256','status','command_id','created_at','finished_at']='{}'
   AND record->>'operation_id'=job.operation_id::text
   AND record->'command'=job.dispatch_command->'command'
   AND candidate_release_timestamp(record->'not_before')=
     candidate_release_timestamp(job.dispatch_command->'not_before')
   AND candidate_release_timestamp(record->'expires_at')=
     candidate_release_timestamp(job.dispatch_command->'expires_at')
   AND record->>'policy_version'='lab-release-dispatch-v1'
   AND record->>'command_sha256'=encode(sha256(convert_to(
     candidate_canonical_json(record->'command'),'UTF8')),'hex')
   AND registered>=job.created_at
   AND registered<candidate_release_timestamp(record->'expires_at')
   AND checked>=registered AND checked<=observed AND observed<=result.created_at
   AND result.created_at-checked<interval '30 seconds'
   AND result.created_at-observed<interval '30 seconds'
   AND jsonb_typeof(receipt)='object'
   AND receipt ?& ARRAY['version','service_name','source','simulation','idempotency_key',
     'status','command','current','effect_current']
   AND receipt-ARRAY['version','service_name','source','simulation','idempotency_key',
     'status','command','current','effect_current']='{}'
   AND receipt->>'version'='lab-release-receipt-v1'
   AND receipt->>'service_name'='order-service' AND receipt->>'source'='local-release-simulation-v1'
   AND receipt->'simulation'='true'::jsonb
   AND receipt->>'idempotency_key'=job.dispatch_command->'command'->>'idempotency_key'
   AND jsonb_typeof(receipt->'effect_current')='boolean'
   AND jsonb_typeof(state)='object'
   AND state ?& ARRAY['service_name','source','simulation','version','revision','command_count',
     'release_500_enabled','updated_at']
   AND state-ARRAY['service_name','source','simulation','version','revision','command_count',
     'release_500_enabled','updated_at']='{}'
   AND state->>'service_name'='order-service' AND state->>'source'='local-release-simulation-v1'
   AND state->'simulation'='true'::jsonb AND state->>'version' IN ('stable','2026.01.01-rc1')
   AND state->'release_500_enabled'=to_jsonb(state->>'version'='2026.01.01-rc1')
   AND jsonb_typeof(state->'revision')='number' AND state->>'revision' ~ '^[1-9][0-9]*$'
   AND (state->>'revision')::numeric BETWEEN 1 AND 2147483647
   AND jsonb_typeof(state->'command_count')='number' AND state->>'command_count' ~ '^[0-9]+$'
   AND (state->>'command_count')::numeric BETWEEN 0 AND 2147483647
   AND (state->>'revision')::numeric<=(state->>'command_count')::numeric+1
   AND (state->>'version'='stable')=((state->>'revision')::numeric % 2=1)
   AND candidate_release_timestamp(state->'updated_at')<=checked) IS NOT TRUE THEN
   RAISE EXCEPTION '发布执行材料形状、来源、时间或窗口不完整'; END IF;
 IF record->>'status'='prepared' THEN
   expected_status:=CASE WHEN checked<candidate_release_timestamp(record->'expires_at')
     THEN 'ready' ELSE 'expired' END;
   IF record->'finished_at'<>'null'::jsonb OR record->'command_id'<>'null'::jsonb THEN
     RAISE EXCEPTION '未终止派发不能有终态引用'; END IF;
 ELSIF record->>'status' IN ('applied','cancelled') THEN
   expected_status:=record->>'status';
   IF (finished>=registered AND finished<=checked) IS NOT TRUE THEN
     RAISE EXCEPTION '派发终态缺少有效时刻'; END IF;
 ELSE RAISE EXCEPTION '派发材料状态不合法'; END IF;
 IF view->>'availability' IS DISTINCT FROM expected_status THEN
   RAISE EXCEPTION '派发材料投影不一致'; END IF;
 IF record->>'status'='applied' THEN
   IF (jsonb_typeof(command)='object'
     AND command ?& ARRAY['id','service_name','sequence','kind','command','command_sha256',
       'previous_version','previous_revision','version','revision','created_at']
     AND command-ARRAY['id','service_name','sequence','kind','command','command_sha256',
       'previous_version','previous_revision','version','revision','created_at']='{}'
     AND candidate_condition_uuid(command->>'id')=record->>'command_id'
     AND command->>'service_name'='order-service' AND command->>'kind'='cas'
     AND command->'command'=record->'command'
     AND command->>'command_sha256'=record->>'command_sha256'
     AND command->>'previous_version'='2026.01.01-rc1' AND command->>'version'='stable'
     AND jsonb_typeof(command->'previous_revision')='number'
     AND command->'previous_revision'=record->'command'->'expected_revision'
     AND jsonb_typeof(command->'revision')='number'
     AND command->>'revision' ~ '^[1-9][0-9]*$'
     AND (command->>'revision')::numeric=(command->>'previous_revision')::numeric+1
     AND (command->>'previous_revision')::numeric % 2=0
     AND jsonb_typeof(command->'sequence')='number'
     AND command->>'sequence' ~ '^[1-9][0-9]*$'
     AND (command->>'sequence')::numeric BETWEEN 1 AND 2147483647
     AND (command->>'previous_revision')::numeric<=(command->>'sequence')::numeric
     AND ((command->>'sequence')::numeric<>1 OR (command->>'previous_revision')::numeric=1)
     AND (command->>'sequence')::numeric<=(state->>'command_count')::numeric
     AND (command->>'revision')::numeric<=(state->>'revision')::numeric
     AND (state->>'revision')::numeric-(command->>'revision')::numeric<=
       (state->>'command_count')::numeric-(command->>'sequence')::numeric
     AND candidate_release_timestamp(command->'created_at')=finished
     AND finished<candidate_release_timestamp(record->'expires_at')
     AND finished<=candidate_release_timestamp(state->'updated_at')
     AND receipt->>'status'='recorded') IS NOT TRUE THEN
     RAISE EXCEPTION '已应用发布缺少原窗口内精确成功回执'; END IF;
   current_effect:=command->'revision'=state->'revision' AND command->'version'=state->'version';
   IF (receipt->'effect_current'=to_jsonb(current_effect)
     AND (outcome=CASE WHEN current_effect THEN 'applied_current' ELSE 'applied_superseded' END)
     AND ((command->'sequence'<>state->'command_count') OR
       (current_effect AND candidate_release_timestamp(command->'created_at')=
         candidate_release_timestamp(state->'updated_at')))) IS NOT TRUE THEN
     RAISE EXCEPTION '历史发布效果与当前适用性不一致'; END IF;
 ELSE
   IF (record->'command_id'='null'::jsonb AND command='null'::jsonb
     AND receipt->>'status'='not_found' AND receipt->'effect_current'='false'::jsonb
     AND ((outcome='not_applied' AND expected_status IN ('expired','cancelled'))
       OR outcome IN ('blocked','unknown'))) IS NOT TRUE THEN
     RAISE EXCEPTION '未应用派发不能声明成功或越过有效窗口结算'; END IF;
 END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION guard_release_result() RETURNS trigger AS $$
DECLARE job candidate_release_execution_jobs%ROWTYPE; attempt
  candidate_release_execution_attempts%ROWTYPE;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '发布执行结果不可更新或删除'; END IF;
 SELECT * INTO job FROM candidate_release_execution_jobs WHERE id=NEW.job_id FOR UPDATE;
 SELECT * INTO attempt FROM candidate_release_execution_attempts WHERE id=NEW.attempt_id;
 IF (attempt.job_id=job.id AND attempt.number=job.attempt_count
   AND ((job.status='running' AND job.lease_token=attempt.lease_token
     AND job.locked_by=attempt.worker_id AND NEW.created_at<job.locked_until)
   OR (job.status IN ('succeeded','failed','unknown') AND job.completed_at=NEW.created_at))
   AND (NEW.created_at<attempt.lease_expires_at OR
     (attempt.number=3 AND attempt.status='lease_expired'
       AND NEW.content->>'outcome'=CASE job.phase WHEN 'dispatched' THEN 'unknown' ELSE 'blocked'
  END)))
   IS NOT TRUE THEN RAISE EXCEPTION '迟到或旧发布执行结果不可结算'; END IF;
 IF NEW.created_at<attempt.lease_expires_at AND clock_timestamp()>=attempt.lease_expires_at THEN
   RAISE EXCEPTION '迟到发布执行结果不能回填旧租约时间'; END IF;
 PERFORM assert_release_result(NEW);
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER release_result_guard BEFORE INSERT OR UPDATE OR DELETE
 ON candidate_release_execution_results FOR EACH ROW EXECUTE FUNCTION guard_release_result();

CREATE FUNCTION assert_release_job_history(job candidate_release_execution_jobs) RETURNS VOID AS $$
DECLARE attempt candidate_release_execution_attempts%ROWTYPE;
 result candidate_release_execution_results%ROWTYPE; expected_status TEXT;
BEGIN
 PERFORM assert_release_job(job,TRUE,FALSE);
 IF (SELECT count(*) FROM candidate_release_execution_attempts WHERE job_id=job.id)
     <>job.attempt_count OR EXISTS (SELECT 1 FROM candidate_release_execution_attempts
     WHERE job_id=job.id AND (number>job.attempt_count OR
       lease_expires_at<>started_at+interval '60 seconds' OR
       (number<job.attempt_count AND status<>'lease_expired'))) THEN
   RAISE EXCEPTION '发布执行任务尝试历史不完整'; END IF;
 SELECT * INTO attempt FROM candidate_release_execution_attempts
   WHERE job_id=job.id AND number=job.attempt_count;
 SELECT * INTO result FROM candidate_release_execution_results WHERE job_id=job.id;
 IF job.phase='dispatched' AND NOT EXISTS (SELECT 1 FROM candidate_release_execution_attempts a
   WHERE a.id=job.dispatch_attempt_id AND a.job_id=job.id AND a.started_at<=job.dispatched_at
   AND job.dispatched_at<a.lease_expires_at) THEN
   RAISE EXCEPTION '发布执行任务缺少派发尝试历史'; END IF;
 IF job.status='running' THEN
   IF (attempt.status='running' AND attempt.lease_token=job.lease_token
     AND attempt.worker_id=job.locked_by AND attempt.lease_expires_at=job.locked_until
     AND result.job_id IS NULL) IS NOT TRUE THEN
     RAISE EXCEPTION '发布执行任务当前租约历史不完整'; END IF;
 ELSIF job.status='queued' THEN
   IF job.attempt_count<>0 OR result.job_id IS NOT NULL THEN
     RAISE EXCEPTION '发布执行任务初始历史不完整'; END IF;
 ELSE
   IF result.job_id IS NULL THEN RAISE EXCEPTION '发布执行任务缺少终态结果历史'; END IF;
   PERFORM assert_release_result(result);
   expected_status:=CASE result.content->>'outcome'
     WHEN 'applied_current' THEN 'succeeded' WHEN 'applied_superseded' THEN 'succeeded'
     WHEN 'unknown' THEN 'unknown' ELSE 'failed' END;
   IF (job.status=expected_status AND result.attempt_id=attempt.id
     AND result.created_at=job.completed_at AND attempt.finished_at=job.completed_at
     AND attempt.status=CASE WHEN attempt.lease_expires_at<=job.completed_at THEN 'lease_expired'
       WHEN job.status='succeeded' THEN 'succeeded' ELSE 'failed' END) IS NOT TRUE THEN
     RAISE EXCEPTION '发布执行任务终态历史不完整'; END IF;
 END IF;
END;
$$ LANGUAGE plpgsql;
"""


_STATE = """
CREATE FUNCTION validate_release_job_state() RETURNS trigger AS $$
DECLARE job candidate_release_execution_jobs%ROWTYPE; audit
  candidate_release_execution_job_commands%ROWTYPE;
 attempt candidate_release_execution_attempts%ROWTYPE; result
  candidate_release_execution_results%ROWTYPE;
 job_tx XID; audit_tx XID; attempt_tx XID; result_tx XID; expected_status TEXT;
BEGIN
 IF TG_TABLE_NAME='candidate_release_execution_jobs' THEN
   SELECT * INTO job FROM candidate_release_execution_jobs WHERE id=NEW.id;
 ELSIF TG_TABLE_NAME='candidate_release_execution_job_commands' THEN
   IF NEW.outcome<>'applied' THEN RETURN NULL; END IF;
   SELECT * INTO job FROM candidate_release_execution_jobs WHERE id=NEW.execution_job_id;
 ELSE SELECT * INTO job FROM candidate_release_execution_jobs WHERE id=NEW.job_id;
 END IF;
 PERFORM assert_release_job(job,TRUE,FALSE);
 -- 仅首次prepared转dispatched的事务重验截止；历史查询与后续只读结算不重验旧窗口。
 IF TG_TABLE_NAME='candidate_release_execution_jobs' AND TG_OP='UPDATE' THEN
   IF OLD.phase='prepared' AND NEW.phase='dispatched' THEN
     SELECT * INTO attempt FROM candidate_release_execution_attempts
       WHERE id=job.dispatch_attempt_id;
     IF (clock_timestamp()<candidate_release_timestamp(job.dispatch_command->'expires_at')
       AND clock_timestamp()<attempt.lease_expires_at) IS NOT TRUE THEN
       RAISE EXCEPTION '首次发布派发事务已越过固定窗口或原租约截止'; END IF;
   END IF;
 END IF;
 SELECT xmin INTO job_tx FROM candidate_release_execution_jobs WHERE id=job.id;
 SELECT * INTO audit FROM candidate_release_execution_job_commands WHERE
  authorization_id=job.authorization_id;
 IF TG_TABLE_NAME='candidate_release_execution_job_commands' OR
   (TG_TABLE_NAME='candidate_release_execution_jobs' AND TG_OP='INSERT') THEN
   SELECT xmin INTO audit_tx FROM candidate_release_execution_job_commands WHERE
  cursor=audit.cursor;
   IF job_tx IS DISTINCT FROM audit_tx THEN
     RAISE EXCEPTION '发布执行任务与成功审计必须同事务精确成功命令闭环'; END IF;
   PERFORM assert_release_job(job,TRUE,TRUE);
 END IF;
 IF (SELECT count(*) FROM candidate_release_execution_attempts WHERE
  job_id=job.id)<>job.attempt_count
   OR EXISTS(SELECT 1 FROM candidate_release_execution_attempts WHERE job_id=job.id
     AND number<job.attempt_count AND status<>'lease_expired') THEN
   RAISE EXCEPTION '发布执行尝试数量或前序租约终态不完整'; END IF;
 SELECT * INTO attempt FROM candidate_release_execution_attempts WHERE job_id=job.id AND
  number=job.attempt_count;
 IF job.phase='dispatched' AND NOT EXISTS (SELECT 1 FROM candidate_release_execution_attempts a
   WHERE a.id=job.dispatch_attempt_id AND a.job_id=job.id AND a.started_at<=job.dispatched_at
     AND job.dispatched_at<a.lease_expires_at) THEN
   RAISE EXCEPTION '发布派发缺少原尝试引用'; END IF;
 SELECT * INTO result FROM candidate_release_execution_results WHERE job_id=job.id;
 IF job.status='queued' THEN
   IF job.attempt_count<>0 OR result.job_id IS NOT NULL THEN
     RAISE EXCEPTION 'queued发布任务不能含尝试或结果'; END IF;
 ELSIF job.status='running' THEN
   IF (attempt.status='running' AND attempt.lease_token=job.lease_token
     AND attempt.worker_id=job.locked_by AND attempt.lease_expires_at=job.locked_until
     AND result.job_id IS NULL) IS NOT TRUE THEN
     RAISE EXCEPTION 'running发布任务缺少当前精确尝试或含提前结果'; END IF;
 ELSE
   IF result.job_id IS NULL THEN RAISE EXCEPTION '发布执行终态缺少同事务结果闭环'; END IF;
   PERFORM assert_release_result(result);
   expected_status:=CASE result.content->>'outcome'
     WHEN 'applied_current' THEN 'succeeded' WHEN 'applied_superseded' THEN 'succeeded'
     WHEN 'unknown' THEN 'unknown' ELSE 'failed' END;
   IF (job.status=expected_status AND result.attempt_id=attempt.id
     AND result.created_at=job.completed_at AND attempt.finished_at=job.completed_at
     AND attempt.status=CASE WHEN attempt.lease_expires_at<=job.completed_at THEN 'lease_expired'
       WHEN job.status='succeeded' THEN 'succeeded' ELSE 'failed' END) IS NOT TRUE THEN
     RAISE EXCEPTION '发布任务、尝试与结果终态不一致'; END IF;
   IF attempt.status='lease_expired' THEN
     IF (attempt.number=3 AND result.content->'observation'='null'::jsonb
       AND result.content->>'outcome'=
         CASE job.phase WHEN 'dispatched' THEN 'unknown' ELSE 'blocked' END) IS NOT TRUE THEN
       RAISE EXCEPTION '耗尽租约终态必须绑定第三次过期尝试及无观测结果'; END IF;
   ELSIF clock_timestamp()>=attempt.lease_expires_at THEN
     RAISE EXCEPTION '发布执行终态事务已越过当前尝试租约截止';
   END IF;
   SELECT xmin INTO attempt_tx FROM candidate_release_execution_attempts WHERE id=attempt.id;
   SELECT xmin INTO result_tx FROM candidate_release_execution_results WHERE job_id=job.id;
   IF job_tx IS DISTINCT FROM attempt_tx OR job_tx IS DISTINCT FROM result_tx THEN
     RAISE EXCEPTION '发布执行终态必须与尝试和结果在同一事务闭环'; END IF;
 END IF;
 RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER release_job_state AFTER INSERT OR UPDATE ON
  candidate_release_execution_jobs
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_release_job_state();
CREATE CONSTRAINT TRIGGER release_attempt_state AFTER INSERT OR UPDATE ON
  candidate_release_execution_attempts
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_release_job_state();
CREATE CONSTRAINT TRIGGER release_result_state AFTER INSERT ON candidate_release_execution_results
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_release_job_state();
CREATE CONSTRAINT TRIGGER release_job_command_state AFTER INSERT ON
  candidate_release_execution_job_commands
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_release_job_state();
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM candidate_release_execution_jobs)
          OR EXISTS (SELECT 1 FROM candidate_release_execution_attempts)
          OR EXISTS (SELECT 1 FROM candidate_release_execution_results)
          OR EXISTS (SELECT 1 FROM candidate_release_execution_job_commands) THEN
          RAISE EXCEPTION '不能删除已保存的候选发布执行事实或命令审计'; END IF;
      END $$;
      DROP FUNCTION validate_release_job_state() CASCADE;
      DROP FUNCTION assert_release_job_history(candidate_release_execution_jobs);
      DROP FUNCTION guard_release_result() CASCADE;
      DROP FUNCTION assert_release_result(candidate_release_execution_results);
      DROP FUNCTION guard_release_attempt() CASCADE;
      DROP FUNCTION guard_release_job() CASCADE;
      DROP FUNCTION guard_release_job_command() CASCADE;
      DROP FUNCTION assert_release_job(candidate_release_execution_jobs,BOOLEAN,BOOLEAN);
      DROP FUNCTION assert_release_job_admission(UUID,TEXT,UUID,UUID,INTEGER,TIMESTAMPTZ,BOOLEAN);
      DROP FUNCTION normalize_release_job_command(TEXT);
      ALTER TABLE candidate_release_execution_jobs DROP CONSTRAINT fk_release_job_dispatch_attempt;
      DROP TABLE candidate_release_execution_job_commands;
      DROP TABLE candidate_release_execution_results;
      DROP TABLE candidate_release_execution_attempts;
      DROP TABLE candidate_release_execution_jobs;
    """)
