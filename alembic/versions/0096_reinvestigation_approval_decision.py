"""回流审批请求的可信决定、独立批准阶段及负面有界收敛。"""

# ruff: noqa: E501
from alembic import op

revision = "0096_reflow_approval_decision"
down_revision = "0095_reflow_approval_request"
branch_labels = None
depends_on = None

_TABLES = """
CREATE TABLE reinvestigation_approval_decisions (
  id UUID NOT NULL PRIMARY KEY,
  investigation_run_id UUID NOT NULL,
  candidate_job_id UUID NOT NULL,
  request_id UUID NOT NULL REFERENCES reinvestigation_approval_requests(id) ON DELETE RESTRICT,
  plan_content_sha256 VARCHAR(64) NOT NULL,
  basis_event_cursor BIGINT NOT NULL REFERENCES incident_events(id) ON DELETE RESTRICT,
  credential_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
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
  CONSTRAINT fk_reflow_decisions_job_run FOREIGN KEY(candidate_job_id,investigation_run_id)
    REFERENCES investigation_reflow_candidate_jobs(id,investigation_run_id) ON DELETE RESTRICT,
  CONSTRAINT uq_reflow_decisions_request UNIQUE(request_id),
  CONSTRAINT uq_reflow_decisions_authorization UNIQUE(authorization_id),
  CONSTRAINT uq_reflow_decisions_event UNIQUE(event_cursor),
  CONSTRAINT uq_reflow_decisions_key UNIQUE(investigation_run_id,decided_by_id,idempotency_key),
  CONSTRAINT ck_reflow_decisions_identity CHECK (candidate_job_id=investigation_run_id),
  CONSTRAINT ck_reflow_decisions_basis CHECK (basis_event_cursor>=1 AND event_cursor>basis_event_cursor),
  CONSTRAINT ck_reflow_decisions_credential CHECK (isfinite(credential_expires_at) AND created_at<credential_expires_at),
  CONSTRAINT ck_reflow_decisions_policy CHECK (policy_version='reinvestigation-approval-decision-v1'),
  CONSTRAINT ck_reflow_decisions_outcome CHECK (outcome IN ('approved','rejected','expired')),
  CONSTRAINT ck_reflow_decisions_versions CHECK
    (previous_incident_version>=1 AND incident_version=previous_incident_version+1),
  CONSTRAINT ck_reflow_decisions_hashes CHECK (plan_content_sha256 ~ '^[0-9a-f]{64}$'
    AND command_sha256 ~ '^[0-9a-f]{64}$' AND content_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_reflow_decisions_content CHECK
    (jsonb_typeof(content)='object' AND octet_length(content::text)<=131072),
  CONSTRAINT ck_reflow_decisions_subject CHECK
    (char_length(btrim(decided_by_id)) BETWEEN 1 AND 100),
  CONSTRAINT ck_reflow_decisions_key CHECK (char_length(idempotency_key) BETWEEN 8 AND 100
    AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$')
);
CREATE TABLE reinvestigation_approval_decision_commands (
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
  decision_id UUID REFERENCES reinvestigation_approval_decisions(id) ON DELETE RESTRICT,
  error_code VARCHAR(100),
  error_message VARCHAR(1000),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  credential_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
  CONSTRAINT ck_reflow_decision_commands_identity CHECK (candidate_job_id=investigation_run_id),
  CONSTRAINT ck_reflow_decision_commands_credential CHECK (isfinite(credential_expires_at) AND created_at<credential_expires_at),
  CONSTRAINT uq_reflow_decision_commands_authorization UNIQUE(authorization_id),
  CONSTRAINT ck_reflow_decision_commands_body CHECK
    (jsonb_typeof(command)='object' AND octet_length(command::text)<=32768),
  CONSTRAINT ck_reflow_decision_commands_raw CHECK
    (octet_length(raw_body) BETWEEN 2 AND 65536),
  CONSTRAINT ck_reflow_decision_commands_hash CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_reflow_decision_commands_subject CHECK
    (char_length(btrim(subject_id)) BETWEEN 1 AND 100),
  CONSTRAINT ck_reflow_decision_commands_outcome CHECK
    ((outcome IN ('applied','replayed') AND decision_id IS NOT NULL AND error_code IS NULL
      AND error_message IS NULL AND ((outcome='applied' AND http_status=201)
      OR (outcome='replayed' AND http_status=200)))
    OR (outcome='error' AND decision_id IS NULL AND error_code IS NOT NULL
      AND error_message IS NOT NULL AND http_status IN (404,409))),
  CONSTRAINT ck_reflow_decision_commands_error CHECK
    (error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,100}$')
);
CREATE INDEX ix_reflow_decision_commands_run_cursor
  ON reinvestigation_approval_decision_commands(investigation_run_id,cursor);
"""

_COMMANDS = """
CREATE FUNCTION normalize_reflow_approval_decision_command(raw_body TEXT) RETURNS JSONB AS $$
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

CREATE FUNCTION guard_reflow_approval_decision_command() RETURNS trigger AS $$
DECLARE admission operator_authorizations%ROWTYPE; decision reinvestigation_approval_decisions%ROWTYPE;
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选决定命令审计不可更新或删除'; END IF;
  SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
  IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
    AND admission.role='approver' AND admission.permissions ? 'decide_approval'
    AND admission.subject_id=NEW.subject_id AND admission.method='POST'
    AND admission.operation='decide_reinvestigation_approval'
    AND admission.route=
      '/api/v1/investigations/{run_id}/reinvestigation-candidate/action-plan/approval-request/decision'
    AND admission.resources=jsonb_build_object('run_id',NEW.investigation_run_id::text)
    AND admission.authorized_at<=NEW.created_at AND isfinite(NEW.created_at)
    AND NEW.created_at<=clock_timestamp()+interval '1 second'
    AND NEW.created_at<NEW.credential_expires_at AND clock_timestamp()<NEW.credential_expires_at
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
  IF normalize_reflow_approval_decision_command(NEW.raw_body) IS DISTINCT FROM NEW.command THEN
    RAISE EXCEPTION '候选决定命令原始正文与规范命令不一致';
  END IF;
  IF NEW.outcome IN ('applied','replayed') THEN
    SELECT * INTO decision FROM reinvestigation_approval_decisions WHERE id=NEW.decision_id;
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
        AND decision.created_at=NEW.created_at AND decision.credential_expires_at=NEW.credential_expires_at)
        OR (NEW.outcome='replayed' AND decision.authorization_id<>NEW.authorization_id)))
      IS NOT TRUE THEN RAISE EXCEPTION '候选决定命令结果与原始决定不一致'; END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER reflow_decision_commands_guard BEFORE INSERT OR UPDATE OR DELETE
  ON reinvestigation_approval_decision_commands FOR EACH ROW EXECUTE FUNCTION guard_reflow_approval_decision_command();
"""

_HISTORY = """
CREATE FUNCTION assert_reflow_decision_request_history(request reinvestigation_approval_requests)
 RETURNS VOID AS $$
DECLARE audit reinvestigation_approval_commands%ROWTYPE; admission operator_authorizations%ROWTYPE;
 event incident_events%ROWTYPE;
BEGIN
 -- 只按保存时刻重建历史预算；131要求的父run持槽不变量继续保留。
 PERFORM assert_reflow_approval_request(request,FALSE);
 SELECT * INTO audit FROM reinvestigation_approval_commands WHERE authorization_id=request.authorization_id;
 SELECT * INTO admission FROM operator_authorizations WHERE id=request.authorization_id;
 SELECT * INTO event FROM incident_events WHERE id=request.event_cursor;
 IF (audit.authorization_id=request.authorization_id AND audit.request_id=request.id AND audit.outcome='applied'
   AND audit.http_status=201 AND audit.error_code IS NULL AND audit.error_message IS NULL
   AND audit.investigation_run_id=request.investigation_run_id AND audit.candidate_job_id=request.candidate_job_id
   AND audit.subject_id=request.requested_by_id AND audit.created_at=request.created_at
   AND audit.credential_expires_at=request.credential_expires_at AND audit.created_at<audit.credential_expires_at
   AND audit.command_sha256=request.command_sha256 AND audit.command_sha256=reflow_hash(audit.command)
   AND normalize_reflow_approval_command(audit.raw_body)=audit.command
   AND audit.command->>'plan_content_sha256'=request.plan_content_sha256
   AND audit.command->'expected_incident_version'=to_jsonb(request.previous_incident_version)
   AND audit.command->>'idempotency_key'=request.idempotency_key
   AND event.reason=audit.command->>'reason'
   AND admission.body_sha256=encode(sha256(convert_to(audit.raw_body,'UTF8')),'hex')) IS NOT TRUE THEN
 RAISE EXCEPTION '回流审批决定缺少完整原请求、可信原始命令与成功审计历史'; END IF;
END; $$ LANGUAGE plpgsql;

CREATE FUNCTION reflow_approval_decision_basis_blocker(decision reinvestigation_approval_decisions,at TIMESTAMPTZ)
 RETURNS TEXT AS $$
DECLARE request reinvestigation_approval_requests%ROWTYPE; run investigation_runs%ROWTYPE;
 incident incidents%ROWTYPE; mutation incident_events%ROWTYPE; deadline TIMESTAMPTZ;
BEGIN
 IF decision.outcome<>'approved' THEN RETURN NULL; END IF;
 IF at IS NULL OR NOT isfinite(at) THEN RETURN 'reflow_source_changed'; END IF;
 SELECT * INTO request FROM reinvestigation_approval_requests WHERE id=decision.request_id;
 SELECT * INTO run FROM investigation_runs WHERE id=decision.investigation_run_id;
 SELECT * INTO incident FROM incidents WHERE id=run.incident_id;
 deadline:=run.started_at+make_interval(secs=>(run.call_budget->>'max_duration_seconds')::integer);
 IF at>=deadline THEN RETURN 'run_deadline_exceeded'; END IF;
 IF at>=request.expires_at THEN RETURN 'reflow_approval_expired'; END IF;
 SELECT * INTO mutation FROM incident_events WHERE incident_id=incident.id
   AND event_type IN ('incident.created','incident.status_changed') ORDER BY id DESC LIMIT 1;
 IF (run.status='settled' AND run.candidate_slot_released_at IS NULL
   AND incident.status='mitigating' AND incident.version=decision.incident_version
   AND mutation.id=decision.event_cursor AND mutation.to_status=incident.status
   AND mutation.incident_version=incident.version AND NOT reinvestigation_has_new_alert(run.id)) IS NOT TRUE THEN
 RETURN 'reflow_source_changed'; END IF;
 RETURN NULL;
END; $$ LANGUAGE plpgsql;
"""

_DECISIONS = """
CREATE FUNCTION assert_reflow_approval_decision(decision reinvestigation_approval_decisions,
 require_current BOOLEAN DEFAULT TRUE) RETURNS VOID AS $$
DECLARE request reinvestigation_approval_requests%ROWTYPE; plan reinvestigation_candidate_plans%ROWTYPE;
 run investigation_runs%ROWTYPE; job investigation_reflow_candidate_jobs%ROWTYPE;
 incident incidents%ROWTYPE; event incident_events%ROWTYPE; basis incident_events%ROWTYPE;
 predecessor incident_events%ROWTYPE; mutation incident_events%ROWTYPE;
 admission operator_authorizations%ROWTYPE; expected JSONB; reason TEXT; target_status TEXT; target_action TEXT;
BEGIN
 IF require_current THEN
   PERFORM 1 FROM work_items WHERE investigation_run_id=decision.investigation_run_id FOR UPDATE;
   PERFORM 1 FROM investigation_reflow_candidate_jobs WHERE id=decision.candidate_job_id FOR UPDATE;
   PERFORM lock_reflow_candidate_basis(decision.investigation_run_id);
 END IF;
 SELECT * INTO run FROM investigation_runs WHERE id=decision.investigation_run_id;
 SELECT * INTO job FROM investigation_reflow_candidate_jobs WHERE id=decision.candidate_job_id;
 SELECT * INTO request FROM reinvestigation_approval_requests WHERE id=decision.request_id;
 SELECT * INTO plan FROM reinvestigation_candidate_plans WHERE id=request.plan_id;
 SELECT * INTO incident FROM incidents WHERE id=run.incident_id;
 SELECT * INTO event FROM incident_events WHERE id=decision.event_cursor;
 SELECT * INTO basis FROM incident_events WHERE id=decision.basis_event_cursor;
 SELECT * INTO predecessor FROM incident_events WHERE incident_id=run.incident_id
   AND id<=decision.basis_event_cursor AND event_type IN ('incident.created','incident.status_changed')
   ORDER BY id DESC LIMIT 1;
 target_status:=CASE decision.outcome WHEN 'approved' THEN 'mitigating' ELSE 'investigating' END;
 target_action:=CASE decision.outcome WHEN 'approved' THEN 'approve_plan' WHEN 'rejected' THEN 'reject_plan' ELSE 'expire_approval' END;
 IF (decision.candidate_job_id=decision.investigation_run_id AND job.id=run.id
   AND request.investigation_run_id=run.id AND request.candidate_job_id=job.id
   AND decision.plan_content_sha256=request.plan_content_sha256
   AND decision.previous_incident_version=request.incident_version
   AND decision.incident_version=decision.previous_incident_version+1
   AND decision.policy_version='reinvestigation-approval-decision-v1'
   AND decision.outcome IN ('approved','rejected','expired')
   AND request.created_at<=decision.created_at AND isfinite(decision.created_at)
   AND isfinite(decision.credential_expires_at) AND decision.created_at<decision.credential_expires_at
   AND basis.incident_id=run.incident_id AND basis.occurred_at<=decision.created_at
   AND decision.basis_event_cursor>=request.event_cursor
   AND decision.basis_event_cursor=(SELECT max(id) FROM incident_events
     WHERE incident_id=run.incident_id AND id<decision.event_cursor)
   AND predecessor.id=request.event_cursor
   AND event.incident_id=run.incident_id AND event.event_type='incident.status_changed'
   AND event.actor_type='human' AND event.actor_id=decision.decided_by_id
   AND event.from_status='awaiting_approval' AND event.to_status=target_status
   AND event.incident_version=decision.incident_version AND event.occurred_at=decision.created_at
   AND event.payload=jsonb_build_object('action',target_action)) IS NOT TRUE THEN
 RAISE EXCEPTION '回流审批决定必须绑定精确请求、真实待审批事件前缀与相邻迁移'; END IF;
 PERFORM assert_reflow_decision_request_history(request);
 IF (decision.outcome='expired' AND decision.created_at>=request.expires_at) IS NOT TRUE
   AND (decision.outcome IN ('approved','rejected') AND decision.created_at<request.expires_at) IS NOT TRUE THEN
 RAISE EXCEPTION '回流审批决定动作必须匹配原请求到期时刻'; END IF;
 IF decision.outcome='approved' AND EXISTS(SELECT 1 FROM incident_events
   WHERE incident_id=run.incident_id AND id>request.basis_event_cursor
     AND id<=decision.basis_event_cursor AND event_type='incident.alert_attached') THEN
 RAISE EXCEPTION '回流批准的迁移前依据已出现新告警'; END IF;
 SELECT * INTO admission FROM operator_authorizations WHERE id=decision.authorization_id;
 IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
   AND admission.role='approver' AND admission.permissions ? 'decide_approval'
   AND admission.subject_id=decision.decided_by_id AND admission.method='POST'
   AND admission.operation='decide_reinvestigation_approval'
   AND admission.route='/api/v1/investigations/{run_id}/reinvestigation-candidate/action-plan/approval-request/decision'
   AND admission.resources=jsonb_build_object('run_id',run.id::text)
   AND admission.authorized_at<=decision.created_at) IS NOT TRUE THEN
 RAISE EXCEPTION '回流审批决定缺少同源可信审批者准入'; END IF;
 expected:=jsonb_build_object('version','reinvestigation-approval-decision-content-v1',
   'request_id',request.id::text,'plan_id',plan.id::text,'plan_content_sha256',plan.content_sha256,
   'outcome',decision.outcome,'plan',plan.content,'execution_eligible',FALSE);
 IF decision.content IS DISTINCT FROM expected OR decision.content_sha256 IS DISTINCT FROM reflow_hash(expected) THEN
 RAISE EXCEPTION '回流审批决定不能改写完整新草案、未决条件或执行资格'; END IF;
 IF require_current THEN
   SELECT * INTO mutation FROM incident_events WHERE incident_id=run.incident_id
     AND event_type IN ('incident.created','incident.status_changed') ORDER BY id DESC LIMIT 1;
   IF (incident.status=target_status AND incident.version=decision.incident_version
     AND incident.updated_at=decision.created_at AND mutation.id=decision.event_cursor
     AND decision.created_at<=clock_timestamp()+interval '1 second'
     AND clock_timestamp()<decision.credential_expires_at
     AND ((decision.outcome='expired' AND clock_timestamp()>=request.expires_at)
       OR (decision.outcome IN ('approved','rejected') AND clock_timestamp()<request.expires_at))) IS NOT TRUE THEN
   RAISE EXCEPTION '回流审批决定提交时实际阶段、动作时限或本次凭据已失效'; END IF;
   IF decision.outcome='approved' THEN
     reason:=reflow_approval_decision_basis_blocker(decision,clock_timestamp());
     IF reason IS NOT NULL THEN RAISE EXCEPTION '回流批准阶段依据拒绝提交: %',reason; END IF;
     reason:=reflow_candidate_budget_blocker(run.id,NULL,NULL);
     IF reason IS NOT NULL THEN RAISE EXCEPTION '回流批准原累计预算拒绝准入: %',reason; END IF;
   END IF;
 END IF;
END; $$ LANGUAGE plpgsql;

CREATE FUNCTION guard_reflow_approval_decision() RETURNS trigger AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '回流审批决定不可更新或删除'; END IF;
 PERFORM assert_reflow_approval_decision(NEW,TRUE);
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER reflow_approval_decisions_guard BEFORE INSERT OR UPDATE OR DELETE ON reinvestigation_approval_decisions
 FOR EACH ROW EXECUTE FUNCTION guard_reflow_approval_decision();

CREATE FUNCTION guard_reflow_decision_incident() RETURNS trigger AS $$
BEGIN
 IF ROW(NEW.status,NEW.version) IS DISTINCT FROM ROW(OLD.status,OLD.version)
   AND EXISTS(SELECT 1 FROM reinvestigation_approval_requests a JOIN investigation_runs r ON r.id=a.investigation_run_id
     WHERE r.incident_id=OLD.id AND a.incident_version=OLD.version) THEN
   IF (OLD.status='awaiting_approval' AND NEW.status IN ('mitigating','investigating')
     AND NEW.version=OLD.version+1) IS NOT TRUE THEN
   RAISE EXCEPTION '回流审批决定必须来自真实待审批阶段及相邻版本'; END IF;
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER reflow_decision_incident_guard BEFORE UPDATE ON incidents
 FOR EACH ROW EXECUTE FUNCTION guard_reflow_decision_incident();

CREATE FUNCTION guard_reflow_decision_event() RETURNS trigger AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM reinvestigation_approval_decisions WHERE event_cursor=OLD.id OR basis_event_cursor=OLD.id) THEN
 RAISE EXCEPTION '回流审批决定绑定的迁移及来源事件不可更新或删除'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER reflow_decision_event_guard BEFORE UPDATE OR DELETE ON incident_events
 FOR EACH ROW EXECUTE FUNCTION guard_reflow_decision_event();
"""

_STATE = """
CREATE FUNCTION validate_reflow_approval_decision_state() RETURNS trigger AS $$
DECLARE decision reinvestigation_approval_decisions%ROWTYPE;
 audit reinvestigation_approval_decision_commands%ROWTYPE; event incident_events%ROWTYPE;
 decision_tx XID; audit_tx XID; event_tx XID; incident_tx XID;
BEGIN
 IF TG_TABLE_NAME='reinvestigation_approval_decisions' THEN
   SELECT * INTO decision FROM reinvestigation_approval_decisions WHERE id=NEW.id;
 ELSE
   SELECT * INTO audit FROM reinvestigation_approval_decision_commands WHERE cursor=NEW.cursor;
   IF clock_timestamp()>=audit.credential_expires_at THEN
   RAISE EXCEPTION '回流审批决定命令提交前本次凭据已过期'; END IF;
   IF audit.outcome<>'applied' THEN RETURN NULL; END IF;
   SELECT * INTO decision FROM reinvestigation_approval_decisions WHERE id=audit.decision_id;
 END IF;
 SELECT * INTO audit FROM reinvestigation_approval_decision_commands WHERE authorization_id=decision.authorization_id;
 SELECT * INTO event FROM incident_events WHERE id=decision.event_cursor;
 IF (decision.id IS NOT NULL AND audit.authorization_id=decision.authorization_id AND audit.outcome='applied'
   AND audit.decision_id=decision.id AND audit.created_at=decision.created_at
   AND audit.investigation_run_id=decision.investigation_run_id AND audit.candidate_job_id=decision.candidate_job_id
   AND audit.subject_id=decision.decided_by_id AND audit.command_sha256=decision.command_sha256
   AND audit.credential_expires_at=decision.credential_expires_at
   AND event.reason=audit.command->>'reason') IS NOT TRUE THEN
 RAISE EXCEPTION '回流审批决定缺少精确成功命令及事故原因闭环'; END IF;
 SELECT xmin INTO decision_tx FROM reinvestigation_approval_decisions WHERE id=decision.id;
 SELECT xmin INTO audit_tx FROM reinvestigation_approval_decision_commands WHERE cursor=audit.cursor;
 SELECT xmin INTO event_tx FROM incident_events WHERE id=decision.event_cursor;
 SELECT i.xmin INTO incident_tx FROM incidents i JOIN investigation_runs r ON r.incident_id=i.id
   WHERE r.id=decision.investigation_run_id;
 IF decision_tx IS DISTINCT FROM audit_tx OR decision_tx IS DISTINCT FROM event_tx
   OR decision_tx IS DISTINCT FROM incident_tx THEN
 RAISE EXCEPTION '回流审批决定、事故迁移、事件和成功命令必须同事务提交'; END IF;
 PERFORM assert_reflow_approval_decision(decision,TRUE);
 RETURN NULL;
END; $$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER reflow_approval_decisions_state AFTER INSERT ON reinvestigation_approval_decisions
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_reflow_approval_decision_state();
CREATE CONSTRAINT TRIGGER reflow_decision_commands_state AFTER INSERT ON reinvestigation_approval_decision_commands
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_reflow_approval_decision_state();

CREATE FUNCTION validate_reflow_decision_incident() RETURNS trigger AS $$
DECLARE request reinvestigation_approval_requests%ROWTYPE; decision reinvestigation_approval_decisions%ROWTYPE;
BEGIN
 IF ROW(NEW.status,NEW.version) IS NOT DISTINCT FROM ROW(OLD.status,OLD.version) THEN RETURN NULL; END IF;
 FOR request IN SELECT a.* FROM reinvestigation_approval_requests a JOIN investigation_runs r ON r.id=a.investigation_run_id
   WHERE r.incident_id=OLD.id AND a.incident_version=OLD.version LOOP
   SELECT * INTO decision FROM reinvestigation_approval_decisions WHERE request_id=request.id
     AND previous_incident_version=OLD.version AND incident_version=NEW.version AND created_at=NEW.updated_at;
   IF decision.id IS NULL THEN RAISE EXCEPTION '回流审批请求退出待审批阶段不能缺少同事务决定与审计'; END IF;
   PERFORM assert_reflow_approval_decision(decision,TRUE);
   IF (SELECT xmin FROM reinvestigation_approval_decisions WHERE id=decision.id)
     IS DISTINCT FROM (SELECT xmin FROM incidents WHERE id=NEW.id) THEN
   RAISE EXCEPTION '回流审批事故迁移不能借用旧决定'; END IF;
 END LOOP;
 RETURN NULL;
END; $$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER reflow_decision_incident_state AFTER UPDATE ON incidents
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_reflow_decision_incident();
"""


def upgrade() -> None:
    for sql in (_TABLES, _COMMANDS, _HISTORY, _DECISIONS, _STATE):
        op.execute(sql)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM reinvestigation_approval_decisions)
        OR EXISTS(SELECT 1 FROM reinvestigation_approval_decision_commands) THEN
      RAISE EXCEPTION '不能删除已保存的回流审批决定或命令审计'; END IF;
    END $$;
    DROP FUNCTION validate_reflow_decision_incident() CASCADE;
    DROP FUNCTION validate_reflow_approval_decision_state() CASCADE;
    DROP FUNCTION guard_reflow_decision_event() CASCADE;
    DROP FUNCTION guard_reflow_decision_incident() CASCADE;
    DROP FUNCTION guard_reflow_approval_decision() CASCADE;
    DROP FUNCTION assert_reflow_approval_decision(reinvestigation_approval_decisions,BOOLEAN);
    DROP FUNCTION reflow_approval_decision_basis_blocker(reinvestigation_approval_decisions,TIMESTAMPTZ);
    DROP FUNCTION assert_reflow_decision_request_history(reinvestigation_approval_requests);
    DROP FUNCTION guard_reflow_approval_decision_command() CASCADE;
    DROP FUNCTION normalize_reflow_approval_decision_command(TEXT);
    DROP TABLE reinvestigation_approval_decision_commands;
    DROP TABLE reinvestigation_approval_decisions;
    """)
