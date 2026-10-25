"""版本化候选执行条件、可信来源与不可变命令闭环。"""

from alembic import op

revision = "0077_candidate_conditions"
down_revision = "0076_candidate_preflight"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(_COMMANDS)
    op.execute(_VERIFICATIONS)
    op.execute(_STATE)


_TABLES = """

CREATE TABLE candidate_condition_verifications (
 id UUID NOT NULL PRIMARY KEY,
 version INTEGER NOT NULL,
 previous_id UUID REFERENCES candidate_condition_verifications(id) ON DELETE RESTRICT,
 previous_content_sha256 VARCHAR(64),
 investigation_run_id UUID NOT NULL,
 candidate_job_id UUID NOT NULL,
 decision_id UUID NOT NULL REFERENCES candidate_approval_decisions(id) ON DELETE RESTRICT,
 decision_content_sha256 VARCHAR(64) NOT NULL,
 incident_version INTEGER NOT NULL,
 policy_version VARCHAR(40) NOT NULL,
 verified_by_id VARCHAR(100) NOT NULL,
 authorization_id UUID NOT NULL REFERENCES operator_authorizations(id) ON DELETE RESTRICT,
 idempotency_key VARCHAR(100) NOT NULL,
 command_sha256 VARCHAR(64) NOT NULL,
 content JSONB NOT NULL,
 content_sha256 VARCHAR(64) NOT NULL,
 created_at TIMESTAMP WITH TIME ZONE NOT NULL,
 expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
 CONSTRAINT fk_candidate_condition_job_run FOREIGN KEY(candidate_job_id,investigation_run_id)
 REFERENCES investigation_candidate_evaluation_jobs(id,investigation_run_id) ON DELETE RESTRICT,
 CONSTRAINT uq_candidate_condition_version UNIQUE(decision_id,version),
 CONSTRAINT uq_candidate_condition_authorization UNIQUE(authorization_id),
 CONSTRAINT uq_candidate_condition_key UNIQUE(investigation_run_id,verified_by_id,idempotency_key),
 CONSTRAINT ck_candidate_condition_policy CHECK
   (policy_version='candidate-condition-verification-v1'),
 CONSTRAINT ck_candidate_condition_version CHECK (incident_version>=2 AND version BETWEEN 1 AND 8),
 CONSTRAINT ck_candidate_condition_previous CHECK
   ((version=1 AND previous_id IS NULL AND previous_content_sha256 IS NULL) OR
    (version>1 AND previous_id IS NOT NULL AND previous_content_sha256 ~ '^[0-9a-f]{64}$')),
 CONSTRAINT ck_candidate_condition_hashes CHECK (decision_content_sha256 ~ '^[0-9a-f]{64}$'
   AND command_sha256 ~ '^[0-9a-f]{64}$' AND content_sha256 ~ '^[0-9a-f]{64}$'),
 CONSTRAINT ck_candidate_condition_content CHECK
   (jsonb_typeof(content)='object' AND octet_length(content::text)<=65536),
 CONSTRAINT ck_candidate_condition_subject CHECK
   (char_length(btrim(verified_by_id)) BETWEEN 1 AND 100),
 CONSTRAINT ck_candidate_condition_key CHECK (char_length(idempotency_key) BETWEEN 8 AND 100
   AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'),
 CONSTRAINT ck_candidate_condition_expiry CHECK
   (expires_at>created_at AND expires_at<=created_at+interval '300 seconds')
);
CREATE INDEX ix_candidate_condition_run_job_version ON candidate_condition_verifications
 (investigation_run_id,candidate_job_id,version);
CREATE TABLE candidate_condition_commands (
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
  verification_id UUID REFERENCES candidate_condition_verifications(id) ON DELETE RESTRICT,
  error_code VARCHAR(100),
  error_message VARCHAR(1000),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  CONSTRAINT uq_candidate_condition_commands_authorization UNIQUE(authorization_id),
  CONSTRAINT ck_candidate_condition_commands_body CHECK
    (jsonb_typeof(command)='object' AND
     octet_length(convert_to(candidate_canonical_json(command),'UTF8'))<=24576),
  CONSTRAINT ck_candidate_condition_commands_raw CHECK
    (octet_length(raw_body) BETWEEN 2 AND 65536),
  CONSTRAINT ck_candidate_condition_commands_hash CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_candidate_condition_commands_subject CHECK
    (char_length(btrim(subject_id)) BETWEEN 1 AND 100),
  CONSTRAINT ck_candidate_condition_commands_outcome CHECK
    ((outcome IN ('applied','replayed') AND verification_id IS NOT NULL AND error_code IS NULL
      AND error_message IS NULL AND ((outcome='applied' AND http_status=201)
      OR (outcome='replayed' AND http_status=200)))
    OR (outcome='error' AND verification_id IS NULL AND error_code IS NOT NULL
      AND error_message IS NOT NULL AND http_status IN (404,409))),
  CONSTRAINT ck_candidate_condition_commands_error CHECK
    (error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,100}$')
);
CREATE INDEX ix_candidate_condition_commands_run_cursor
  ON candidate_condition_commands(investigation_run_id,cursor);
"""

_COMMANDS = """
CREATE FUNCTION candidate_condition_uuid(value TEXT) RETURNS TEXT AS $$
DECLARE pattern TEXT;
BEGIN
 pattern:='[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}';
 IF value ~ ('^urn:uuid:'||pattern||'$') THEN value:=substr(value,10);
 ELSIF value !~ ('^('||pattern||'|[0-9a-fA-F]{32}|[{]'||pattern||'[}])$') THEN
   RETURN NULL;
 END IF;
 RETURN value::uuid::text;
EXCEPTION WHEN invalid_text_representation THEN RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

CREATE FUNCTION normalize_candidate_condition_command(raw_body TEXT) RETURNS JSONB AS $$
DECLARE body JSONB; item JSONB; source JSONB; normalized_source JSONB;
 items JSONB:='[]'; sources JSONB; normalized_item JSONB; result JSONB;
 decision_text TEXT; preflight_text TEXT; value TEXT; observed TIMESTAMPTZ;
 seen_items JSONB:='[]'; seen_preflights JSONB:='[]';
BEGIN
 body:=raw_body::jsonb;
 IF (jsonb_typeof(body)='object'
   AND body ?& ARRAY['decision_id','decision_content_sha256','expected_incident_version',
     'expected_condition_version','previous_content_sha256','items','idempotency_key','reason']
   AND (body-ARRAY['decision_id','decision_content_sha256','expected_incident_version',
     'expected_condition_version','previous_content_sha256','items','idempotency_key','reason'])='{}'
   AND jsonb_typeof(body->'decision_id')='string'
   AND jsonb_typeof(body->'decision_content_sha256')='string'
   AND jsonb_typeof(body->'idempotency_key')='string'
   AND jsonb_typeof(body->'reason')='string'
   AND jsonb_typeof(body->'expected_incident_version')='number'
   AND raw_body::json->>'expected_incident_version' ~ '^[0-9]+$'
   AND (body->>'expected_incident_version')::numeric>=2
   AND jsonb_typeof(body->'expected_condition_version')='number'
   AND raw_body::json->>'expected_condition_version' ~ '^[0-9]+$'
   AND (body->>'expected_condition_version')::numeric BETWEEN 0 AND 7
   AND ((body->'expected_condition_version'='0'::jsonb
      AND body->'previous_content_sha256'='null'::jsonb)
     OR (body->'expected_condition_version'<>'0'::jsonb
      AND jsonb_typeof(body->'previous_content_sha256')='string'
      AND candidate_strip_text(body->>'previous_content_sha256') ~ '^[0-9a-f]{64}$'))
   AND candidate_strip_text(body->>'decision_content_sha256') ~ '^[0-9a-f]{64}$'
   AND char_length(candidate_strip_text(body->>'idempotency_key')) BETWEEN 8 AND 100
   AND candidate_strip_text(body->>'idempotency_key') ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'
   AND char_length(candidate_strip_text(body->>'reason')) BETWEEN 1 AND 500
   AND jsonb_typeof(body->'items')='array'
   AND jsonb_array_length(body->'items') BETWEEN 1 AND 20) IS NOT TRUE THEN RETURN NULL;
 END IF;
 decision_text:=candidate_condition_uuid(body->>'decision_id');
 IF decision_text IS NULL THEN RETURN NULL; END IF;
 FOR item IN SELECT jsonb_array_elements(body->'items') LOOP
   IF (jsonb_typeof(item)='object'
     AND item ?& ARRAY['condition_id','status','explanation','sources']
     AND (item-ARRAY['condition_id','status','explanation','sources'])='{}'
     AND jsonb_typeof(item->'condition_id')='string'
     AND candidate_strip_text(item->>'condition_id') ~ '^[0-9a-f]{64}$'
     AND jsonb_typeof(item->'status')='string'
     AND item->>'status' IN ('verified','unresolved','not_applicable')
     AND jsonb_typeof(item->'explanation')='string'
     AND char_length(candidate_strip_text(item->>'explanation')) BETWEEN 1 AND 500
     AND jsonb_typeof(item->'sources')='array'
     AND jsonb_array_length(item->'sources') BETWEEN 0 AND 3
     AND (item->>'status'='unresolved'
          OR jsonb_array_length(item->'sources')>0)) IS NOT TRUE THEN RETURN NULL; END IF;
   value:=candidate_strip_text(item->>'condition_id');
   IF seen_items ? value THEN RETURN NULL; END IF;
   seen_items:=seen_items||to_jsonb(value);
   sources:='[]';
   FOR source IN SELECT jsonb_array_elements(item->'sources') LOOP
     IF jsonb_typeof(source)<>'object' OR jsonb_typeof(source->'kind')<>'string'
       THEN RETURN NULL; END IF;
     -- 判别器与 Pydantic 相同，先判别再裁剪联合字段。
     CASE source->>'kind'
     WHEN 'evidence' THEN
       IF (source ?& ARRAY['kind','evidence_id']
         AND (source-ARRAY['kind','evidence_id'])='{}'
         AND jsonb_typeof(source->'evidence_id')='string'
         AND char_length(candidate_strip_text(source->>'evidence_id')) BETWEEN 1 AND 100
         AND candidate_strip_text(source->>'evidence_id') ~ '^[a-z0-9][a-z0-9-]*$')
         IS NOT TRUE THEN RETURN NULL; END IF;
       normalized_source:=jsonb_build_object('kind','evidence',
         'evidence_id',candidate_strip_text(source->>'evidence_id'));
     WHEN 'human_observation' THEN
       IF (source ?& ARRAY['kind','text','observed_at']
         AND (source-ARRAY['kind','text','observed_at'])='{}'
         AND jsonb_typeof(source->'text')='string'
         AND char_length(candidate_strip_text(source->>'text')) BETWEEN 1 AND 500
         AND jsonb_typeof(source->'observed_at')='string'
         AND source->>'observed_at' ~
           ('^[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]'||
            '([.][0-9]{1,6})?(Z|[+-](0[0-9]|1[0-5]):[0-5][0-9])$'))
         IS NOT TRUE THEN RETURN NULL; END IF;
       observed:=(source->>'observed_at')::timestamptz;
       IF extract(year FROM observed AT TIME ZONE 'UTC') NOT BETWEEN 1 AND 9999 THEN
         RETURN NULL; END IF;
       value:=to_char(observed AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS');
       IF to_char(observed AT TIME ZONE 'UTC','US')<>'000000' THEN
         value:=value||'.'||to_char(observed AT TIME ZONE 'UTC','US');
       END IF;
       normalized_source:=jsonb_build_object('kind','human_observation',
         'text',candidate_strip_text(source->>'text'),'observed_at',value||'Z');
     WHEN 'preflight' THEN
       IF (source ?& ARRAY['kind','preflight_id','content_sha256']
         AND (source-ARRAY['kind','preflight_id','content_sha256'])='{}'
         AND jsonb_typeof(source->'preflight_id')='string'
         AND jsonb_typeof(source->'content_sha256')='string'
         AND candidate_strip_text(source->>'content_sha256') ~ '^[0-9a-f]{64}$')
         IS NOT TRUE THEN RETURN NULL; END IF;
       preflight_text:=candidate_condition_uuid(source->>'preflight_id');
       IF preflight_text IS NULL THEN RETURN NULL; END IF;
       IF NOT seen_preflights ? preflight_text THEN
         seen_preflights:=seen_preflights||to_jsonb(preflight_text);
         IF jsonb_array_length(seen_preflights)>3 THEN RETURN NULL; END IF;
       END IF;
       normalized_source:=jsonb_build_object('kind','preflight','preflight_id',preflight_text,
         'content_sha256',candidate_strip_text(source->>'content_sha256'));
     ELSE RETURN NULL;
     END CASE;
     IF sources @> jsonb_build_array(normalized_source) THEN RETURN NULL; END IF;
     sources:=sources||jsonb_build_array(normalized_source);
   END LOOP;
   normalized_item:=jsonb_build_object('condition_id',candidate_strip_text(item->>'condition_id'),
     'status',item->>'status',
     'explanation',candidate_strip_text(item->>'explanation'),'sources',sources);
   items:=items||jsonb_build_array(normalized_item);
 END LOOP;
 result:=jsonb_build_object('decision_id',decision_text,
   'decision_content_sha256',candidate_strip_text(body->>'decision_content_sha256'),
   'expected_incident_version',body->'expected_incident_version',
   'expected_condition_version',body->'expected_condition_version',
   'previous_content_sha256',CASE WHEN body->'previous_content_sha256'='null'::jsonb
     THEN NULL ELSE candidate_strip_text(body->>'previous_content_sha256') END,
   'items',items,'idempotency_key',candidate_strip_text(body->>'idempotency_key'),
   'reason',candidate_strip_text(body->>'reason'));
 IF octet_length(convert_to(candidate_canonical_json(result),'UTF8'))>24576 THEN
   RETURN NULL;
 END IF;
 RETURN result;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range OR
 datetime_field_overflow OR invalid_datetime_format THEN RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

CREATE FUNCTION guard_candidate_condition_command() RETURNS trigger AS $$
DECLARE admission operator_authorizations%ROWTYPE;
 verification candidate_condition_verifications%ROWTYPE;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选条件命令审计不可更新或删除'; END IF;
 SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id FOR UPDATE;
 IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
   AND admission.role='engineer' AND admission.permissions ? 'verify_action'
   AND admission.subject_id=NEW.subject_id AND admission.method='POST'
   AND admission.operation='verify_candidate_conditions'
   AND admission.route=
     '/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-conditions'
   AND admission.resources=jsonb_build_object('run_id',NEW.investigation_run_id::text,
     'job_id',NEW.candidate_job_id::text)
   AND admission.authorized_at<=NEW.created_at
   AND admission.body_sha256=encode(sha256(convert_to(NEW.raw_body,'UTF8')),'hex'))
   IS NOT TRUE THEN RAISE EXCEPTION '候选条件命令缺少精确原始正文或同源可信准入'; END IF;
 IF normalize_candidate_condition_command(NEW.raw_body) IS DISTINCT FROM NEW.command THEN
   RAISE EXCEPTION '候选条件命令原始正文与规范命令不一致';
 END IF;
 IF NEW.command_sha256 IS DISTINCT FROM encode(sha256(convert_to(
   candidate_canonical_json(NEW.command),'UTF8')),'hex') THEN
   RAISE EXCEPTION '候选条件命令缺少精确规范摘要';
 END IF;
 IF NEW.outcome='error' AND EXISTS (SELECT 1 FROM candidate_condition_verifications v
   WHERE v.investigation_run_id=NEW.investigation_run_id AND v.verified_by_id=NEW.subject_id
     AND v.candidate_job_id=NEW.candidate_job_id
     AND v.idempotency_key=NEW.command->>'idempotency_key'
     AND v.command_sha256=NEW.command_sha256) THEN
   RAISE EXCEPTION '已有候选条件核验事实不能降级为业务拒绝审计';
 END IF;
 IF NEW.outcome IN ('applied','replayed') THEN
   SELECT * INTO verification FROM candidate_condition_verifications WHERE id=NEW.verification_id;
   IF (verification.id IS NOT NULL
     AND verification.investigation_run_id=NEW.investigation_run_id
     AND verification.candidate_job_id=NEW.candidate_job_id
     AND verification.verified_by_id=NEW.subject_id
     AND verification.command_sha256=NEW.command_sha256
     AND verification.decision_id::text=NEW.command->>'decision_id'
     AND verification.decision_content_sha256=NEW.command->>'decision_content_sha256'
     AND to_jsonb(verification.incident_version)=NEW.command->'expected_incident_version'
     AND to_jsonb(verification.version-1)=NEW.command->'expected_condition_version'
     AND COALESCE(to_jsonb(verification.previous_content_sha256),'null'::jsonb)=
         NEW.command->'previous_content_sha256'
     AND verification.idempotency_key=NEW.command->>'idempotency_key'
     AND verification.created_at<=NEW.created_at
     AND ((NEW.outcome='applied' AND verification.authorization_id=NEW.authorization_id
       AND verification.created_at=NEW.created_at)
       OR (NEW.outcome='replayed' AND verification.authorization_id<>NEW.authorization_id)))
     IS NOT TRUE THEN RAISE EXCEPTION '候选条件命令结果与核验事实不一致'; END IF;
   IF NEW.outcome='replayed' THEN
     PERFORM assert_candidate_condition_verification(verification,FALSE,FALSE);
   END IF;
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_condition_commands_guard BEFORE INSERT OR UPDATE OR DELETE
 ON candidate_condition_commands FOR EACH ROW EXECUTE FUNCTION guard_candidate_condition_command();
"""

_VERIFICATIONS = """
CREATE FUNCTION assert_candidate_condition_snapshot(
 verification candidate_condition_verifications, decision candidate_approval_decisions,
 plan candidate_action_plans, require_audit BOOLEAN)
RETURNS VOID AS $$
DECLARE admission operator_authorizations%ROWTYPE; audit candidate_condition_commands%ROWTYPE;
 preflight candidate_execution_preflights%ROWTYPE; paudit candidate_preflight_commands%ROWTYPE;
 padmission operator_authorizations%ROWTYPE; item JSONB; source JSONB; condition JSONB;
 items JSONB:='[]'; catalog JSONB:='[]'; command_items JSONB:='[]';
 original TEXT; kind TEXT; position BIGINT; text_hash TEXT; condition_id TEXT;
 status TEXT:='ready_for_review'; expiration TIMESTAMPTZ; observed TIMESTAMPTZ;
 expected JSONB; body JSONB; idx INTEGER:=0;
BEGIN
 SELECT * INTO admission FROM operator_authorizations WHERE id=verification.authorization_id;
 SELECT * INTO audit FROM candidate_condition_commands
   WHERE authorization_id=verification.authorization_id;
 IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
   AND admission.role='engineer' AND admission.permissions ? 'verify_action'
   AND admission.subject_id=verification.verified_by_id AND admission.method='POST'
   AND admission.operation='verify_candidate_conditions'
   AND admission.route=
     '/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-conditions'
   AND admission.resources=jsonb_build_object('run_id',verification.investigation_run_id::text,
     'job_id',verification.candidate_job_id::text)
   AND admission.authorized_at<=verification.created_at) IS NOT TRUE THEN
   RAISE EXCEPTION '候选条件缺少同源可信工程师准入'; END IF;
 IF (verification.decision_id=decision.id
   AND verification.decision_content_sha256=decision.content_sha256
   AND verification.investigation_run_id=decision.investigation_run_id
   AND verification.candidate_job_id=decision.candidate_job_id
   AND verification.incident_version=decision.incident_version
   AND decision.created_at<=verification.created_at) IS NOT TRUE THEN
   RAISE EXCEPTION '候选条件缺少精确原批准归属'; END IF;
 FOR kind IN SELECT unnest(ARRAY['precondition','question']) LOOP
   FOR original,position IN SELECT value,ordinality FROM jsonb_array_elements_text(
     plan.content->CASE kind WHEN 'precondition' THEN 'preconditions'
       ELSE 'unresolved_questions' END) WITH ORDINALITY LOOP
     text_hash:=encode(sha256(convert_to(original,'UTF8')),'hex');
     condition_id:=encode(sha256(convert_to(candidate_canonical_json(jsonb_build_object(
       'plan_id',plan.id::text,'plan_content_sha256',plan.content_sha256,
       'kind',kind,'position',position,'text_sha256',text_hash)),'UTF8')),'hex');
     condition:=jsonb_build_object('condition_id',condition_id,'kind',kind,
       'position',position,'text',original,'text_sha256',text_hash);
     catalog:=catalog||jsonb_build_array(condition);
   END LOOP;
 END LOOP;
 IF (jsonb_typeof(verification.content->'items')='array'
   AND jsonb_array_length(verification.content->'items')=jsonb_array_length(catalog))
   IS NOT TRUE THEN RAISE EXCEPTION '候选条件必须覆盖完整原计划目录'; END IF;
 expiration:=least((SELECT expires_at FROM candidate_approval_requests
   WHERE id=decision.request_id),
   verification.created_at+interval '300 seconds');
 FOR item IN SELECT jsonb_array_elements(verification.content->'items') LOOP
   IF (jsonb_typeof(item)='object'
     AND item ?& ARRAY['condition','status','explanation','sources']
     AND (item-ARRAY['condition','status','explanation','sources'])='{}'
     AND item->'condition'=catalog->idx) IS NOT TRUE THEN
     RAISE EXCEPTION '候选条件必须按精确顺序保留原文及稳定目录摘要'; END IF;
   command_items:=command_items||jsonb_build_array(jsonb_build_object(
     'condition_id',item #>> '{condition,condition_id}','status',item->'status',
     'explanation',item->'explanation','sources',item->'sources'));
   IF item->>'status'='unresolved' THEN status:='blocked';
   ELSIF item->>'status'='not_applicable' AND status<>'blocked'
     THEN status:='plan_revision_required'; END IF;
   FOR source IN SELECT jsonb_array_elements(item->'sources') LOOP
     CASE source->>'kind'
     WHEN 'evidence' THEN
       IF NOT decision.content->'evidence_ids' ? (source->>'evidence_id') THEN
         RAISE EXCEPTION '候选条件证据必须来自原批准保留材料'; END IF;
     WHEN 'human_observation' THEN
       observed:=(source->>'observed_at')::timestamptz;
       IF observed<decision.created_at OR observed>verification.created_at THEN
         RAISE EXCEPTION '候选条件人工观察必须在原批准之后且不晚于核验'; END IF;
       expiration:=least(expiration,observed+interval '300 seconds');
     WHEN 'preflight' THEN
       SELECT * INTO preflight FROM candidate_execution_preflights
         WHERE id=(source->>'preflight_id')::uuid;
       SELECT * INTO paudit FROM candidate_preflight_commands
         WHERE authorization_id=preflight.authorization_id;
       SELECT * INTO padmission FROM operator_authorizations
         WHERE id=preflight.authorization_id;
       IF (preflight.id IS NOT NULL AND preflight.decision_id=decision.id
         AND preflight.decision_content_sha256=decision.content_sha256
         AND preflight.investigation_run_id=verification.investigation_run_id
         AND preflight.candidate_job_id=verification.candidate_job_id
         AND preflight.incident_version=decision.incident_version
         AND preflight.content_sha256=source->>'content_sha256'
         AND preflight.created_at>=decision.created_at
         AND preflight.created_at<=verification.created_at
         AND verification.created_at<preflight.expires_at
         AND preflight.expires_at=least(
           (preflight.content #>> '{observation,observed_at}')::timestamptz+interval '30 seconds',
           (SELECT expires_at FROM candidate_approval_requests WHERE id=decision.request_id))
         AND preflight.content #> '{observation,error_code}'='null'::jsonb
         AND preflight.policy_version='candidate-execution-preflight-v1'
         AND preflight.content_sha256=encode(sha256(convert_to(
           candidate_canonical_json(preflight.content),'UTF8')),'hex')
         AND padmission.policy_version='operator-auth-v1' AND padmission.role='executor'
         AND padmission.permissions ? 'execute_action'
         AND padmission.subject_id=preflight.requested_by_id AND padmission.method='POST'
         AND padmission.operation='check_candidate_execution'
         AND padmission.route=
           '/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-preflights'
         AND padmission.resources=jsonb_build_object(
           'run_id',verification.investigation_run_id::text,
           'job_id',verification.candidate_job_id::text)
         AND padmission.authorized_at<=preflight.created_at
         AND paudit.outcome='applied' AND paudit.http_status=201
         AND paudit.preflight_id=preflight.id AND paudit.subject_id=preflight.requested_by_id
         AND paudit.investigation_run_id=verification.investigation_run_id
         AND paudit.candidate_job_id=verification.candidate_job_id
         AND paudit.created_at=preflight.created_at
         AND paudit.command_sha256=preflight.command_sha256
         AND normalize_candidate_preflight_command(paudit.raw_body)=paudit.command
         AND paudit.command_sha256=encode(sha256(convert_to(
           candidate_canonical_json(paudit.command),'UTF8')),'hex')
         AND padmission.body_sha256=encode(sha256(convert_to(paudit.raw_body,'UTF8')),'hex')
         AND paudit.command->>'decision_id'=decision.id::text
         AND paudit.command->>'decision_content_sha256'=decision.content_sha256
         AND paudit.command->'expected_incident_version'=to_jsonb(decision.incident_version)
         AND paudit.command->>'idempotency_key'=preflight.idempotency_key)
         IS NOT TRUE THEN RAISE EXCEPTION '候选条件预检缺少新鲜同源成功历史'; END IF;
       PERFORM assert_candidate_condition_preflight(preflight,decision,plan);
       expiration:=least(expiration,preflight.expires_at);
     ELSE RAISE EXCEPTION '候选条件来源类型不受支持';
     END CASE;
   END LOOP;
   idx:=idx+1;
 END LOOP;
 -- 重用规范入口验证嵌套形状、严格类型、重复来源和总大小；正文仍由原可信授权绑定。
 body:=jsonb_build_object('decision_id',decision.id::text,
   'decision_content_sha256',decision.content_sha256,
   'expected_incident_version',verification.incident_version,
   'expected_condition_version',verification.version-1,
   'previous_content_sha256',verification.previous_content_sha256,
   'items',command_items,'idempotency_key',verification.idempotency_key,
   'reason',CASE WHEN require_audit THEN audit.command->>'reason' ELSE '核验来源' END);
 IF normalize_candidate_condition_command(candidate_canonical_json(body)) IS DISTINCT FROM body THEN
   RAISE EXCEPTION '候选条件内容不符合严格来源与完整快照契约'; END IF;
 expected:=jsonb_build_object('version','candidate-condition-verification-content-v1',
   'decision_id',decision.id::text,'decision_content_sha256',decision.content_sha256,
   'plan_id',plan.id::text,'plan_content_sha256',plan.content_sha256,
   'items',verification.content->'items','status',status,
   'approval_required',TRUE,'execution_eligible',FALSE);
 IF verification.content IS DISTINCT FROM expected OR verification.content_sha256 IS DISTINCT FROM
   encode(sha256(convert_to(candidate_canonical_json(expected),'UTF8')),'hex') THEN
   RAISE EXCEPTION '候选条件内容不能改写原计划、汇总状态或审批执行资格'; END IF;
 IF verification.expires_at IS DISTINCT FROM expiration OR verification.created_at>=expiration THEN
   RAISE EXCEPTION '候选条件核验期限必须精确保留最短有效来源期限'; END IF;
 IF require_audit AND (audit.outcome='applied' AND audit.http_status=201
   AND audit.verification_id=verification.id AND audit.subject_id=verification.verified_by_id
   AND audit.investigation_run_id=verification.investigation_run_id
   AND audit.candidate_job_id=verification.candidate_job_id
   AND audit.created_at=verification.created_at
   AND audit.command_sha256=verification.command_sha256
   AND normalize_candidate_condition_command(audit.raw_body)=audit.command
   AND audit.command=body AND audit.command_sha256=encode(sha256(convert_to(
     candidate_canonical_json(audit.command),'UTF8')),'hex')
   AND admission.body_sha256=encode(sha256(convert_to(audit.raw_body,'UTF8')),'hex'))
   IS NOT TRUE THEN RAISE EXCEPTION '候选条件版本缺少完整精确成功命令历史'; END IF;
END;
$$ LANGUAGE plpgsql;
"""

_VERIFICATIONS += """
CREATE FUNCTION assert_candidate_condition_preflight(
 preflight candidate_execution_preflights, decision candidate_approval_decisions,
 plan candidate_action_plans) RETURNS VOID AS $$
DECLARE step JSONB; action TEXT; expected_path TEXT; observation JSONB; blockers JSONB;
 observed_at TIMESTAMPTZ; expected_content JSONB;
 request candidate_approval_requests%ROWTYPE;
BEGIN
  SELECT * INTO request FROM candidate_approval_requests WHERE id=decision.request_id;
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


"""

_VERIFICATIONS += """
CREATE FUNCTION assert_candidate_condition_verification(
 verification candidate_condition_verifications, require_latest BOOLEAN,
 require_current BOOLEAN DEFAULT TRUE)
RETURNS VOID AS $$
DECLARE
 decision candidate_approval_decisions%ROWTYPE; request candidate_approval_requests%ROWTYPE;
 plan candidate_action_plans%ROWTYPE; job investigation_candidate_evaluation_jobs%ROWTYPE;
 run investigation_runs%ROWTYPE; incident incidents%ROWTYPE; event incident_events%ROWTYPE;
 admission operator_authorizations%ROWTYPE; decision_admission operator_authorizations%ROWTYPE;
 audit candidate_decision_commands%ROWTYPE; current_version INTEGER; expected_content JSONB;
 prior candidate_condition_verifications%ROWTYPE; latest_version INTEGER;
 expected_version INTEGER:=1; previous_uuid UUID; previous_hash TEXT; previous_created TIMESTAMPTZ;
BEGIN
  IF require_current THEN
    SELECT * INTO admission FROM operator_authorizations
      WHERE id=verification.authorization_id FOR UPDATE;
  END IF;
  PERFORM 1 FROM work_items WHERE investigation_run_id=verification.investigation_run_id FOR UPDATE;
  SELECT * INTO job FROM investigation_candidate_evaluation_jobs
    WHERE id=verification.candidate_job_id FOR UPDATE;
  SELECT * INTO run FROM investigation_runs WHERE id=verification.investigation_run_id;
  SELECT * INTO incident FROM incidents WHERE id=run.incident_id FOR UPDATE;
  SELECT * INTO decision FROM candidate_approval_decisions WHERE id=verification.decision_id;
  SELECT * INTO request FROM candidate_approval_requests WHERE id=decision.request_id;
  SELECT * INTO plan FROM candidate_action_plans WHERE id=request.plan_id;
  IF (decision.id IS NOT NULL AND request.id IS NOT NULL
    AND decision.outcome='approved' AND decision.investigation_run_id=run.id
    AND decision.candidate_job_id=job.id AND job.investigation_run_id=run.id
    AND request.investigation_run_id=run.id AND request.candidate_job_id=job.id
    AND decision.plan_content_sha256=request.plan_content_sha256
    AND decision.previous_incident_version=request.incident_version
    AND decision.incident_version=request.incident_version+1
    AND verification.decision_content_sha256=decision.content_sha256
    AND (NOT require_current OR
      (incident.status='mitigating' AND incident.version=verification.incident_version))
    AND verification.incident_version=decision.incident_version
    AND request.created_at<=decision.created_at AND decision.created_at<=verification.created_at
    AND verification.created_at<request.expires_at AND decision.created_at<request.expires_at
    AND verification.created_at<run.started_at+
      make_interval(secs=>(run.call_budget->>'max_duration_seconds')::int)) IS NOT TRUE THEN
    RAISE EXCEPTION '候选条件需要精确当前批准、事故状态版本及原有效期限';
  END IF;
  -- 历史授权只读；不能锁定旧准入，不能要求旧决定时的 incident.updated_at。
  -- 完整来源含浮点观测，其散列及 UUID5 身份由应用重建，不新增数据库散列扩展。
  PERFORM assert_candidate_decision_request_history(request,require_current);
  SELECT max(state_version) INTO current_version FROM investigation_human_wait_events
    WHERE investigation_run_id=run.id;
  IF require_current AND ((job.source_kind='terminal_handoff'
      AND run.status='await_candidate') IS NOT TRUE
    AND (job.source_kind='human_route' AND run.status='await_human'
      AND current_version IS NOT DISTINCT FROM request.wait_version
      AND NOT EXISTS (SELECT 1 FROM investigation_human_wait_events h
        WHERE h.investigation_run_id=run.id AND h.action='cancel')) IS NOT TRUE) THEN
    RAISE EXCEPTION '候选条件的原输入已补充、取消或不再适用';
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
    RAISE EXCEPTION '候选条件缺少完整原批准可信命令历史';
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
    RAISE EXCEPTION '候选条件的原批准内容或摘要不完整';
  END IF;
  SELECT * INTO event FROM incident_events WHERE id=decision.event_cursor;
  IF (event.incident_id=incident.id AND event.event_type='incident.status_changed'
    AND event.actor_type='human' AND event.actor_id=decision.decided_by_id
    AND event.from_status='awaiting_approval' AND event.to_status='mitigating'
    AND event.incident_version=decision.incident_version AND event.occurred_at=decision.created_at
    AND event.payload=jsonb_build_object('action','approve_plan')
    AND event.reason=audit.command->>'reason') IS NOT TRUE THEN
    RAISE EXCEPTION '候选条件缺少精确原批准迁移事件';
  END IF;

 SELECT max(version) INTO latest_version FROM candidate_condition_verifications
   WHERE decision_id=decision.id AND id<>verification.id;
 IF require_latest AND verification.version<>COALESCE(latest_version,0)+1 THEN
   RAISE EXCEPTION '候选条件版本必须CAS精确最新前驱'; END IF;
 FOR prior IN SELECT * FROM candidate_condition_verifications
   WHERE decision_id=decision.id AND (NOT require_current OR version<verification.version)
   ORDER BY version LOOP
   IF (prior.version=expected_version AND prior.previous_id IS NOT DISTINCT FROM previous_uuid
     AND prior.previous_content_sha256 IS NOT DISTINCT FROM previous_hash
     AND (previous_created IS NULL OR prior.created_at>=previous_created)) IS NOT TRUE THEN
     RAISE EXCEPTION '候选条件版本前缀不连续或前驱已损坏'; END IF;
   PERFORM assert_candidate_condition_snapshot(prior,decision,plan,TRUE);
   previous_uuid:=prior.id; previous_hash:=prior.content_sha256; previous_created:=prior.created_at;
   expected_version:=expected_version+1;
 END LOOP;
 IF require_current AND ((verification.version=expected_version
   AND verification.previous_id IS NOT DISTINCT FROM previous_uuid
   AND verification.previous_content_sha256 IS NOT DISTINCT FROM previous_hash
   AND (previous_created IS NULL OR verification.created_at>=previous_created)) IS NOT TRUE) THEN
   RAISE EXCEPTION '候选条件版本缺少精确完整前驱'; END IF;
 PERFORM assert_candidate_condition_snapshot(verification,decision,plan,NOT require_current);
END;
$$ LANGUAGE plpgsql;
CREATE FUNCTION guard_candidate_condition_verification() RETURNS trigger AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选条件核验不可更新或删除'; END IF;
 PERFORM assert_candidate_condition_verification(NEW,TRUE);
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_condition_verifications_guard BEFORE INSERT OR UPDATE OR DELETE
 ON candidate_condition_verifications FOR EACH ROW
 EXECUTE FUNCTION guard_candidate_condition_verification();
"""

_STATE = """
CREATE FUNCTION validate_candidate_condition_state() RETURNS trigger AS $$
DECLARE verification candidate_condition_verifications%ROWTYPE;
 audit candidate_condition_commands%ROWTYPE; verification_tx XID; audit_tx XID;
 decision candidate_approval_decisions%ROWTYPE; plan candidate_action_plans%ROWTYPE;
BEGIN
 IF TG_TABLE_NAME='candidate_condition_verifications' THEN
   SELECT * INTO verification FROM candidate_condition_verifications WHERE id=NEW.id;
 ELSIF NEW.outcome='applied' THEN
   SELECT * INTO verification FROM candidate_condition_verifications WHERE id=NEW.verification_id;
 ELSE RETURN NULL;
 END IF;
 SELECT * INTO audit FROM candidate_condition_commands
   WHERE authorization_id=verification.authorization_id;
 IF (verification.id IS NOT NULL AND audit.outcome='applied'
   AND audit.verification_id=verification.id AND audit.created_at=verification.created_at
   AND audit.subject_id=verification.verified_by_id
   AND audit.investigation_run_id=verification.investigation_run_id
   AND audit.candidate_job_id=verification.candidate_job_id
   AND audit.command_sha256=verification.command_sha256) IS NOT TRUE THEN
   RAISE EXCEPTION '候选条件缺少同事务精确成功命令闭环'; END IF;
 SELECT xmin INTO verification_tx FROM candidate_condition_verifications WHERE id=verification.id;
 SELECT xmin INTO audit_tx FROM candidate_condition_commands WHERE cursor=audit.cursor;
 IF verification_tx IS DISTINCT FROM audit_tx THEN
   RAISE EXCEPTION '候选条件与成功命令必须同事务精确绑定'; END IF;
 PERFORM assert_candidate_condition_verification(verification,FALSE);
 SELECT * INTO decision FROM candidate_approval_decisions WHERE id=verification.decision_id;
 SELECT p.* INTO plan FROM candidate_action_plans p JOIN candidate_approval_requests r
   ON r.plan_id=p.id WHERE r.id=decision.request_id;
 PERFORM assert_candidate_condition_snapshot(verification,decision,plan,TRUE);
 RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER candidate_condition_verifications_state
 AFTER INSERT ON candidate_condition_verifications DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION validate_candidate_condition_state();
CREATE CONSTRAINT TRIGGER candidate_condition_commands_state
 AFTER INSERT ON candidate_condition_commands DEFERRABLE INITIALLY DEFERRED
 FOR EACH ROW EXECUTE FUNCTION validate_candidate_condition_state();
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM candidate_condition_verifications)
          OR EXISTS (SELECT 1 FROM candidate_condition_commands) THEN
          RAISE EXCEPTION '不能删除已保存的候选条件核验事实或命令审计';
        END IF;
      END $$;
      DROP TRIGGER candidate_condition_verifications_state ON candidate_condition_verifications;
      DROP TRIGGER candidate_condition_commands_state ON candidate_condition_commands;
      DROP FUNCTION validate_candidate_condition_state();
      DROP TRIGGER candidate_condition_verifications_guard ON candidate_condition_verifications;
      DROP FUNCTION guard_candidate_condition_verification();
      DROP FUNCTION assert_candidate_condition_verification(
        candidate_condition_verifications,BOOLEAN,BOOLEAN);
      DROP FUNCTION assert_candidate_condition_snapshot(candidate_condition_verifications,
        candidate_approval_decisions,candidate_action_plans,BOOLEAN);
      DROP FUNCTION assert_candidate_condition_preflight(candidate_execution_preflights,
        candidate_approval_decisions,candidate_action_plans);
      DROP TRIGGER candidate_condition_commands_guard ON candidate_condition_commands;
      DROP FUNCTION guard_candidate_condition_command();
      DROP FUNCTION normalize_candidate_condition_command(TEXT);
      DROP FUNCTION candidate_condition_uuid(TEXT);
      DROP TABLE candidate_condition_commands;
      DROP TABLE candidate_condition_verifications;
    """)
