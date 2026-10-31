"""回流新草案的可信审批请求、独立阶段依据与事故迁移闭环。"""

# ruff: noqa: E501
from alembic import op

revision = "0095_reflow_approval_request"
down_revision = "0094_reflow_candidate_plan"
branch_labels = None
depends_on = None

_TABLES = """
CREATE TABLE reinvestigation_approval_requests (
  id UUID NOT NULL PRIMARY KEY,
  investigation_run_id UUID NOT NULL,
  candidate_job_id UUID NOT NULL,
  plan_id UUID NOT NULL,
  plan_content_sha256 VARCHAR(64) NOT NULL,
  basis_event_cursor BIGINT NOT NULL REFERENCES incident_events(id) ON DELETE RESTRICT,
  credential_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
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
  CONSTRAINT fk_reflow_approval_plan_run FOREIGN KEY(plan_id,investigation_run_id)
    REFERENCES reinvestigation_candidate_plans(id,investigation_run_id) ON DELETE RESTRICT,
  CONSTRAINT fk_reflow_approval_job_run FOREIGN KEY(candidate_job_id,investigation_run_id)
    REFERENCES investigation_reflow_candidate_jobs(id,investigation_run_id) ON DELETE RESTRICT,
  CONSTRAINT uq_reflow_approval_plan UNIQUE(plan_id),
  CONSTRAINT uq_reflow_approval_authorization UNIQUE(authorization_id),
  CONSTRAINT uq_reflow_approval_event UNIQUE(event_cursor),
  CONSTRAINT uq_reflow_approval_key UNIQUE(investigation_run_id,requested_by_id,idempotency_key),
  CONSTRAINT ck_reflow_approval_policy CHECK (policy_version='reinvestigation-approval-v1'),
  CONSTRAINT ck_reflow_approval_status CHECK (status='pending'),
  CONSTRAINT ck_reflow_approval_identity CHECK (candidate_job_id=investigation_run_id),
  CONSTRAINT ck_reflow_approval_basis CHECK (basis_event_cursor>=1 AND event_cursor>basis_event_cursor),
  CONSTRAINT ck_reflow_approval_credential CHECK (isfinite(credential_expires_at) AND created_at<credential_expires_at),
  CONSTRAINT ck_reflow_approval_versions CHECK
    (previous_incident_version>=1 AND incident_version=previous_incident_version+1),
  CONSTRAINT ck_reflow_approval_hashes CHECK (plan_content_sha256 ~ '^[0-9a-f]{64}$'
    AND command_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_reflow_approval_subject CHECK
    (char_length(btrim(requested_by_id)) BETWEEN 1 AND 100),
  CONSTRAINT ck_reflow_approval_key CHECK (char_length(idempotency_key) BETWEEN 8 AND 100
    AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'),
  CONSTRAINT ck_reflow_approval_expiry CHECK
    (expires_at>created_at AND expires_at<=created_at+interval '30 minutes')
);
CREATE TABLE reinvestigation_approval_commands (
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
  request_id UUID REFERENCES reinvestigation_approval_requests(id) ON DELETE RESTRICT,
  error_code VARCHAR(100),
  error_message VARCHAR(1000),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  credential_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
  CONSTRAINT ck_reflow_approval_commands_identity CHECK (candidate_job_id=investigation_run_id),
  CONSTRAINT ck_reflow_approval_commands_credential CHECK (isfinite(credential_expires_at) AND created_at<credential_expires_at),
  CONSTRAINT uq_reflow_approval_commands_authorization UNIQUE(authorization_id),
  CONSTRAINT ck_reflow_approval_commands_body CHECK
    (jsonb_typeof(command)='object' AND octet_length(command::text)<=32768),
  CONSTRAINT ck_reflow_approval_commands_raw CHECK
    (octet_length(raw_body) BETWEEN 2 AND 65536),
  CONSTRAINT ck_reflow_approval_commands_hash CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_reflow_approval_commands_subject CHECK
    (char_length(btrim(subject_id)) BETWEEN 1 AND 100),
  CONSTRAINT ck_reflow_approval_commands_outcome CHECK
    ((outcome IN ('applied','replayed') AND request_id IS NOT NULL AND error_code IS NULL
      AND error_message IS NULL AND ((outcome='applied' AND http_status=201)
      OR (outcome='replayed' AND http_status=200)))
    OR (outcome='error' AND request_id IS NULL AND error_code IS NOT NULL
      AND error_message IS NOT NULL AND http_status IN (404,409))),
  CONSTRAINT ck_reflow_approval_commands_error CHECK
    (error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,100}$')
);
CREATE INDEX ix_reflow_approval_commands_run_cursor
  ON reinvestigation_approval_commands(investigation_run_id,cursor);
"""

_COMMANDS = """
CREATE FUNCTION normalize_reflow_approval_command(raw_body TEXT) RETURNS JSONB AS $$
DECLARE body JSONB;
BEGIN
  body:=raw_body::jsonb;
  IF (jsonb_typeof(body)='object'
    AND body ?& ARRAY['plan_content_sha256',
                     'expected_incident_version','acknowledge_unresolved_conditions','idempotency_key','reason']
    AND (body-ARRAY['action','plan_content_sha256',
                   'expected_incident_version','acknowledge_unresolved_conditions','idempotency_key','reason'])='{}'::jsonb
    AND (NOT body ? 'action' OR (jsonb_typeof(body->'action')='string'
                                AND body->>'action'='request_approval'))
    AND jsonb_typeof(body->'plan_content_sha256')='string'
    AND jsonb_typeof(body->'idempotency_key')='string'
    AND jsonb_typeof(body->'reason')='string'
    AND body->'acknowledge_unresolved_conditions'='true'::jsonb
    AND jsonb_typeof(body->'expected_incident_version')='number'
    AND raw_body::json->>'expected_incident_version' ~ '^[0-9]+$'
    AND (body->>'expected_incident_version')::numeric>=1) IS NOT TRUE THEN
    RETURN NULL;
  END IF;
  RETURN jsonb_build_object('action','request_approval',
    'plan_content_sha256',candidate_strip_text(body->>'plan_content_sha256'),
    'expected_incident_version',body->'expected_incident_version',
    'acknowledge_unresolved_conditions',TRUE,
    'idempotency_key',candidate_strip_text(body->>'idempotency_key'),
    'reason',candidate_strip_text(body->>'reason'));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
  RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

CREATE FUNCTION guard_reflow_approval_command() RETURNS trigger AS $$
DECLARE
  admission operator_authorizations%ROWTYPE;
  request reinvestigation_approval_requests%ROWTYPE;
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选审批命令审计不可更新或删除'; END IF;
  SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
  IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
    AND admission.role='engineer'
    AND admission.permissions ?& ARRAY['request_approval','manage_incident']
    AND admission.subject_id=NEW.subject_id AND admission.method='POST'
    AND admission.operation='create_reinvestigation_approval_request'
    AND admission.route=
      '/api/v1/investigations/{run_id}/reinvestigation-candidate/action-plan/approval-request'
    AND admission.resources=jsonb_build_object('run_id',NEW.investigation_run_id::text)
    AND admission.authorized_at<=NEW.created_at AND isfinite(NEW.created_at)
    AND NEW.created_at<=clock_timestamp()+interval '1 second'
    AND NEW.created_at<NEW.credential_expires_at AND clock_timestamp()<NEW.credential_expires_at
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
  IF normalize_reflow_approval_command(NEW.raw_body) IS DISTINCT FROM NEW.command THEN
    RAISE EXCEPTION '候选审批命令原始正文与规范命令不一致';
  END IF;
  IF NEW.outcome IN ('applied','replayed') THEN
    SELECT * INTO request FROM reinvestigation_approval_requests WHERE id=NEW.request_id;
    IF (request.id IS NOT NULL AND request.investigation_run_id=NEW.investigation_run_id
      AND request.candidate_job_id=NEW.candidate_job_id AND request.requested_by_id=NEW.subject_id
      AND request.command_sha256=NEW.command_sha256
      AND request.plan_content_sha256=NEW.command->>'plan_content_sha256'
      AND to_jsonb(request.previous_incident_version)=NEW.command->'expected_incident_version'
      AND request.idempotency_key=NEW.command->>'idempotency_key'
      AND request.created_at<=NEW.created_at
      AND ((NEW.outcome='applied' AND request.authorization_id=NEW.authorization_id
            AND request.created_at=NEW.created_at AND request.credential_expires_at=NEW.credential_expires_at)
        OR (NEW.outcome='replayed' AND request.authorization_id<>NEW.authorization_id))) IS NOT TRUE
    THEN RAISE EXCEPTION '候选审批命令结果与原始审批请求不一致'; END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER reflow_approval_commands_guard BEFORE INSERT OR UPDATE OR DELETE
  ON reinvestigation_approval_commands FOR EACH ROW EXECUTE FUNCTION guard_reflow_approval_command();
"""

_HISTORY = """
CREATE FUNCTION assert_reflow_approval_plan_history(plan reinvestigation_candidate_plans)
 RETURNS VOID AS $$
DECLARE audit reinvestigation_candidate_plan_commands%ROWTYPE; admission operator_authorizations%ROWTYPE;
BEGIN
 -- 当前审批阶段已合法改变事故状态，历史草案不能再要求 investigating/current。
 PERFORM assert_reflow_plan(plan,FALSE);
 SELECT * INTO audit FROM reinvestigation_candidate_plan_commands WHERE authorization_id=plan.authorization_id;
 SELECT * INTO admission FROM operator_authorizations WHERE id=plan.authorization_id;
 IF (audit.authorization_id=plan.authorization_id AND audit.plan_id=plan.id AND audit.outcome='applied'
   AND audit.http_status=201 AND audit.error_code IS NULL AND audit.error_message IS NULL
   AND audit.investigation_run_id=plan.investigation_run_id AND audit.candidate_job_id=plan.candidate_job_id
   AND audit.subject_id=plan.requested_by_id AND audit.created_at=plan.created_at
   AND audit.credential_expires_at=plan.credential_expires_at
   AND audit.created_at<audit.credential_expires_at
   AND audit.command_sha256=plan.command_sha256 AND audit.command_sha256=reflow_hash(audit.command)
   AND normalize_reflow_plan_command(audit.raw_body)=audit.command
   AND audit.command->>'candidate_result_sha256'=plan.candidate_result_sha256
   AND audit.command->>'routing_sha256'=plan.routing_sha256
   AND audit.command->'expected_incident_version'=to_jsonb(plan.incident_version)
   AND audit.command->>'idempotency_key'=plan.idempotency_key
   AND admission.body_sha256=encode(sha256(convert_to(audit.raw_body,'UTF8')),'hex')) IS NOT TRUE THEN
 RAISE EXCEPTION '回流审批请求缺少完整草案、可信原始命令及成功审计历史'; END IF;
END; $$ LANGUAGE plpgsql;

CREATE FUNCTION reflow_approval_basis_blocker(request reinvestigation_approval_requests,at TIMESTAMPTZ)
 RETURNS TEXT AS $$
DECLARE run investigation_runs%ROWTYPE; incident incidents%ROWTYPE;
 admission candidate_reinvestigation_admissions%ROWTYPE; mutation incident_events%ROWTYPE;
 deadline TIMESTAMPTZ;
BEGIN
 IF at IS NULL OR NOT isfinite(at) THEN RETURN 'reflow_source_changed'; END IF;
 SELECT * INTO run FROM investigation_runs WHERE id=request.investigation_run_id;
 SELECT * INTO incident FROM incidents WHERE id=run.incident_id;
 SELECT * INTO admission FROM candidate_reinvestigation_admissions WHERE investigation_run_id=run.id;
 deadline:=run.started_at+make_interval(secs=>(run.call_budget->>'max_duration_seconds')::integer);
 IF at>=deadline THEN RETURN 'run_deadline_exceeded'; END IF;
 IF at>=request.expires_at THEN RETURN 'reflow_approval_expired'; END IF;
 SELECT * INTO mutation FROM incident_events WHERE incident_id=incident.id
   AND event_type IN ('incident.created','incident.status_changed') ORDER BY id DESC LIMIT 1;
 IF (run.status='settled' AND run.candidate_slot_released_at IS NULL
   AND to_jsonb(request.previous_incident_version)=admission.content->'incident_version'
   AND incident.status='awaiting_approval' AND incident.version=request.incident_version
   AND mutation.id=request.event_cursor AND mutation.to_status=incident.status
   AND mutation.incident_version=incident.version AND NOT reinvestigation_has_new_alert(run.id)) IS NOT TRUE THEN
 RETURN 'reflow_source_changed'; END IF;
 RETURN NULL;
END; $$ LANGUAGE plpgsql;
"""

_REQUESTS = """
CREATE FUNCTION assert_reflow_approval_request(request reinvestigation_approval_requests,
 require_current BOOLEAN DEFAULT TRUE) RETURNS VOID AS $$
DECLARE plan reinvestigation_candidate_plans%ROWTYPE; job investigation_reflow_candidate_jobs%ROWTYPE;
 run investigation_runs%ROWTYPE; incident incidents%ROWTYPE; event incident_events%ROWTYPE;
 basis incident_events%ROWTYPE; admission operator_authorizations%ROWTYPE; reason TEXT;
BEGIN
 IF require_current THEN
   PERFORM 1 FROM work_items WHERE investigation_run_id=request.investigation_run_id FOR UPDATE;
   PERFORM 1 FROM investigation_reflow_candidate_jobs WHERE id=request.candidate_job_id FOR UPDATE;
   PERFORM lock_reflow_candidate_basis(request.investigation_run_id);
 END IF;
 SELECT * INTO run FROM investigation_runs WHERE id=request.investigation_run_id;
 SELECT * INTO job FROM investigation_reflow_candidate_jobs WHERE id=request.candidate_job_id;
 SELECT * INTO plan FROM reinvestigation_candidate_plans WHERE id=request.plan_id;
 SELECT * INTO incident FROM incidents WHERE id=run.incident_id;
 SELECT * INTO event FROM incident_events WHERE id=request.event_cursor;
 SELECT * INTO basis FROM incident_events WHERE id=request.basis_event_cursor;
 IF (request.candidate_job_id=request.investigation_run_id AND job.id=run.id
   AND plan.investigation_run_id=run.id AND plan.candidate_job_id=job.id
   AND request.plan_content_sha256=plan.content_sha256 AND plan.created_at<=request.created_at
   AND plan.incident_version=request.previous_incident_version
   AND request.incident_version=request.previous_incident_version+1
   AND request.policy_version='reinvestigation-approval-v1' AND request.status='pending'
   AND isfinite(request.created_at) AND isfinite(request.expires_at) AND isfinite(request.credential_expires_at)
   AND request.created_at<request.expires_at AND request.created_at<request.credential_expires_at
   AND request.created_at<run.started_at+make_interval(secs=>(run.call_budget->>'max_duration_seconds')::integer)
   AND request.expires_at=least(request.created_at+interval '30 minutes',
     run.started_at+make_interval(secs=>(run.call_budget->>'max_duration_seconds')::integer))
   AND basis.incident_id=run.incident_id AND basis.occurred_at<=request.created_at
   AND request.basis_event_cursor=(SELECT max(id) FROM incident_events
     WHERE incident_id=run.incident_id AND id<request.event_cursor)
   AND event.incident_id=run.incident_id AND event.event_type='incident.status_changed'
   AND event.actor_type='human' AND event.actor_id=request.requested_by_id
   AND event.from_status='investigating' AND event.to_status='awaiting_approval'
   AND event.incident_version=request.incident_version AND event.occurred_at=request.created_at
   AND event.payload=jsonb_build_object('action','request_approval')) IS NOT TRUE THEN
 RAISE EXCEPTION '回流审批请求必须绑定精确草案、原截止、迁移前实际事件前缀和新阶段事件'; END IF;
 PERFORM assert_reflow_approval_plan_history(plan);
 -- 原前缀仍须有效，只豁免本请求自身合法迁移，不能豁免真实告警或更早状态变化。
 IF reflow_execution_historical_basis(run.id,request.basis_event_cursor) IS NOT NULL THEN
 RAISE EXCEPTION '回流审批请求之前的原调查依据已经变化'; END IF;
 SELECT * INTO admission FROM operator_authorizations WHERE id=request.authorization_id;
 IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
   AND admission.role='engineer' AND admission.permissions ?& ARRAY['request_approval','manage_incident']
   AND admission.subject_id=request.requested_by_id AND admission.method='POST'
   AND admission.operation='create_reinvestigation_approval_request'
   AND admission.route='/api/v1/investigations/{run_id}/reinvestigation-candidate/action-plan/approval-request'
   AND admission.resources=jsonb_build_object('run_id',run.id::text)
   AND admission.authorized_at<=request.created_at) IS NOT TRUE THEN
 RAISE EXCEPTION '回流审批请求缺少同源可信工程师准入'; END IF;
 IF require_current THEN
   IF request.created_at>clock_timestamp()+interval '1 second'
     OR clock_timestamp()>=request.credential_expires_at THEN
   RAISE EXCEPTION '回流审批请求提交时本次操作者凭据已失效'; END IF;
   reason:=reflow_approval_basis_blocker(request,clock_timestamp());
   IF reason IS NOT NULL THEN RAISE EXCEPTION '回流审批阶段依据拒绝提交: %',reason; END IF;
   IF incident.updated_at IS DISTINCT FROM request.created_at THEN
   RAISE EXCEPTION '回流审批请求缺少本次真实事故迁移'; END IF;
   reason:=reflow_candidate_budget_blocker(run.id,NULL,NULL);
   IF reason IS NOT NULL THEN RAISE EXCEPTION '回流审批原累计预算拒绝准入: %',reason; END IF;
 END IF;
END; $$ LANGUAGE plpgsql;

CREATE FUNCTION guard_reflow_approval_request() RETURNS trigger AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '回流审批请求不可更新或删除'; END IF;
 PERFORM assert_reflow_approval_request(NEW,TRUE);
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER reflow_approval_requests_guard BEFORE INSERT OR UPDATE OR DELETE ON reinvestigation_approval_requests
 FOR EACH ROW EXECUTE FUNCTION guard_reflow_approval_request();

CREATE FUNCTION guard_reflow_approval_incident() RETURNS trigger AS $$
BEGIN
 IF NEW.status='awaiting_approval' AND ROW(NEW.status,NEW.version) IS DISTINCT FROM ROW(OLD.status,OLD.version)
   AND EXISTS(SELECT 1 FROM reinvestigation_candidate_plans p JOIN investigation_runs r ON r.id=p.investigation_run_id
     WHERE r.incident_id=OLD.id AND p.incident_version=OLD.version) THEN
   IF (OLD.status='investigating' AND NEW.version=OLD.version+1) IS NOT TRUE THEN
   RAISE EXCEPTION '回流审批迁移必须来自真实 investigating 状态及相邻版本'; END IF;
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER reflow_approval_incident_guard BEFORE UPDATE ON incidents
 FOR EACH ROW EXECUTE FUNCTION guard_reflow_approval_incident();

CREATE FUNCTION guard_reflow_approval_event() RETURNS trigger AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM reinvestigation_approval_requests
   WHERE event_cursor=OLD.id OR basis_event_cursor=OLD.id) THEN
 RAISE EXCEPTION '回流审批请求绑定的迁移及来源事件不可更新或删除'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER reflow_approval_event_guard BEFORE UPDATE OR DELETE ON incident_events
 FOR EACH ROW EXECUTE FUNCTION guard_reflow_approval_event();
"""

_STATE = """
CREATE FUNCTION validate_reflow_approval_state() RETURNS trigger AS $$
DECLARE request reinvestigation_approval_requests%ROWTYPE; audit reinvestigation_approval_commands%ROWTYPE;
 event incident_events%ROWTYPE; request_tx XID; event_tx XID; incident_tx XID; audit_tx XID;
BEGIN
 IF TG_TABLE_NAME='reinvestigation_approval_requests' THEN
   SELECT * INTO request FROM reinvestigation_approval_requests WHERE id=NEW.id;
 ELSE
   SELECT * INTO audit FROM reinvestigation_approval_commands WHERE cursor=NEW.cursor;
   IF clock_timestamp()>=audit.credential_expires_at THEN
   RAISE EXCEPTION '回流审批命令提交前本次操作者凭据已过期'; END IF;
   IF audit.outcome<>'applied' THEN RETURN NULL; END IF;
   SELECT * INTO request FROM reinvestigation_approval_requests WHERE id=audit.request_id;
 END IF;
 SELECT * INTO audit FROM reinvestigation_approval_commands WHERE authorization_id=request.authorization_id;
 SELECT * INTO event FROM incident_events WHERE id=request.event_cursor;
 IF (request.id IS NOT NULL AND audit.authorization_id=request.authorization_id AND audit.outcome='applied'
   AND audit.request_id=request.id AND audit.created_at=request.created_at
   AND audit.investigation_run_id=request.investigation_run_id AND audit.candidate_job_id=request.candidate_job_id
   AND audit.subject_id=request.requested_by_id AND audit.command_sha256=request.command_sha256
   AND audit.credential_expires_at=request.credential_expires_at
   AND event.reason=audit.command->>'reason') IS NOT TRUE THEN
 RAISE EXCEPTION '回流审批请求缺少精确成功命令及事故事件原因闭环'; END IF;
 SELECT xmin INTO request_tx FROM reinvestigation_approval_requests WHERE id=request.id;
 SELECT xmin INTO event_tx FROM incident_events WHERE id=request.event_cursor;
 SELECT xmin INTO audit_tx FROM reinvestigation_approval_commands WHERE cursor=audit.cursor;
 SELECT i.xmin INTO incident_tx FROM incidents i JOIN investigation_runs r ON r.incident_id=i.id
   WHERE r.id=request.investigation_run_id;
 IF request_tx IS DISTINCT FROM event_tx OR request_tx IS DISTINCT FROM audit_tx
   OR request_tx IS DISTINCT FROM incident_tx THEN
 RAISE EXCEPTION '回流审批请求、事故迁移、事件和成功命令必须同事务提交'; END IF;
 PERFORM assert_reflow_approval_request(request,TRUE);
 RETURN NULL;
END; $$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER reflow_approval_requests_state AFTER INSERT ON reinvestigation_approval_requests
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_reflow_approval_state();
CREATE CONSTRAINT TRIGGER reflow_approval_commands_state AFTER INSERT ON reinvestigation_approval_commands
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_reflow_approval_state();

CREATE FUNCTION validate_reflow_approval_incident() RETURNS trigger AS $$
DECLARE plan reinvestigation_candidate_plans%ROWTYPE; request reinvestigation_approval_requests%ROWTYPE;
BEGIN
 IF NEW.status<>'awaiting_approval' OR ROW(NEW.status,NEW.version) IS NOT DISTINCT FROM ROW(OLD.status,OLD.version)
 THEN RETURN NULL; END IF;
 FOR plan IN SELECT p.* FROM reinvestigation_candidate_plans p JOIN investigation_runs r ON r.id=p.investigation_run_id
   WHERE r.incident_id=OLD.id AND p.incident_version=OLD.version LOOP
   SELECT * INTO request FROM reinvestigation_approval_requests WHERE plan_id=plan.id
     AND previous_incident_version=OLD.version AND incident_version=NEW.version AND created_at=NEW.updated_at;
   IF request.id IS NULL THEN RAISE EXCEPTION '回流草案的审批事故迁移不能缺少同事务请求与审计'; END IF;
   PERFORM assert_reflow_approval_request(request,TRUE);
   -- 复用完整闭环触发器之外的独立检查，禁止孤立迁移借用旧请求行。
   IF (SELECT xmin FROM reinvestigation_approval_requests WHERE id=request.id)
     IS DISTINCT FROM (SELECT xmin FROM incidents WHERE id=NEW.id) THEN
   RAISE EXCEPTION '回流审批事故迁移不能借用旧请求'; END IF;
 END LOOP;
 RETURN NULL;
END; $$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER reflow_approval_incident_state AFTER UPDATE ON incidents
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_reflow_approval_incident();
"""


def upgrade() -> None:
    for sql in (_TABLES, _COMMANDS, _HISTORY, _REQUESTS, _STATE):
        op.execute(sql)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM reinvestigation_approval_requests)
        OR EXISTS(SELECT 1 FROM reinvestigation_approval_commands) THEN
      RAISE EXCEPTION '不能删除已保存的回流审批请求或命令审计'; END IF;
    END $$;
    DROP FUNCTION validate_reflow_approval_incident() CASCADE;
    DROP FUNCTION validate_reflow_approval_state() CASCADE;
    DROP FUNCTION guard_reflow_approval_event() CASCADE;
    DROP FUNCTION guard_reflow_approval_incident() CASCADE;
    DROP FUNCTION guard_reflow_approval_request() CASCADE;
    DROP FUNCTION assert_reflow_approval_request(reinvestigation_approval_requests,BOOLEAN);
    DROP FUNCTION reflow_approval_basis_blocker(reinvestigation_approval_requests,TIMESTAMPTZ);
    DROP FUNCTION assert_reflow_approval_plan_history(reinvestigation_candidate_plans);
    DROP FUNCTION guard_reflow_approval_command() CASCADE;
    DROP FUNCTION normalize_reflow_approval_command(TEXT);
    DROP TABLE reinvestigation_approval_commands;
    DROP TABLE reinvestigation_approval_requests;
    """)
