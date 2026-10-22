"""新增可信人工重评执行归属并接入原共享调用账本。"""

from alembic import op

revision = "0070_human_input_reassessment"
down_revision = "0069_human_input_preparation"
branch_labels = None
depends_on = None


_OLD_CONSTRAINTS = {
    "ck_call_reservations_loop_payload": "((operation IN "
    "('read_log_evidence','read_metric_evidence','read_change_evidence','"
    "read_log_runbook','read_metric_runbook','read_change_runbook')) "
    "= (tool_request IS NOT NULL)) AND (tool_request IS "
    "NULL OR (jsonb_typeof(tool_request) = 'object' AND "
    "octet_length(tool_request::text) <= 8192)) AND "
    "(call_result IS NULL OR (status <> 'reserved' AND "
    "jsonb_typeof(call_result) = 'object' AND "
    "octet_length(call_result::text) <= 65536 AND "
    "COALESCE(call_result ->> 'kind', '') = kind AND "
    "((kind = 'model' AND ((operation = 'coordinate' AND "
    "COALESCE(model_input ->> 'version', '') IN "
    "('coordination-model-input-v1','coordination-experience-input-v1') "
    "AND COALESCE(call_result ->> 'version', '') = "
    "'coordination-model-result-v1') OR (operation = "
    "'synthesize' AND NOT (model_input ? 'version') AND "
    "COALESCE(call_result ->> 'version', '') = "
    "'open-hypothesis-synthesis-result-v1') OR (operation "
    "= 'review' AND NOT (model_input ? 'version') AND "
    "COALESCE(call_result ->> 'version', '') = "
    "'hypothesis-review-result-v1') OR (operation NOT IN "
    "('coordinate', 'synthesize', 'review') AND "
    "COALESCE(model_input ->> 'version', '') IN "
    "('evidence-loop-input-v1','runbook-loop-input-v1')))) "
    "OR (kind = 'tool' AND tool_request IS NOT NULL))))",
    "ck_call_reservations_invocation_index": "invocation_index BETWEEN 0 AND 99 AND (operation "
    "<> 'coordinate' OR invocation_index = 0) AND "
    "(operation <> 'review' OR invocation_index IN (0, "
    "1, 2))",
    "ck_call_reservations_invocation_key": "idempotency_key = investigation_attempt_id::text || "
    "':' || COALESCE(task_attempt_id::text, 'root') || "
    "':' || operation || CASE WHEN invocation_index = 0 "
    "THEN '' ELSE ':' || invocation_index::text END",
    "ck_call_reservations_operation": "(operation IN ('query_logs', 'query_metrics', "
    "'list_changes', 'read_log_evidence', "
    "'read_metric_evidence', 'read_change_evidence', "
    "'read_log_runbook', 'read_metric_runbook', "
    "'read_change_runbook') AND kind = 'tool') OR (operation "
    "IN ('coordinate', 'analyze_log', 'analyze_metric', "
    "'analyze_change', 'synthesize', 'review') AND kind = "
    "'model')",
    "ck_call_reservations_scope": "(operation IN ('coordinate', 'synthesize', 'review') AND "
    "task_id IS NULL AND task_attempt_id IS NULL) OR (operation "
    "NOT IN ('coordinate', 'synthesize', 'review') AND task_id IS "
    "NOT NULL AND task_attempt_id IS NOT NULL)",
}

_NEW_CONSTRAINTS = {
    "ck_call_reservations_loop_payload": "((operation IN "
    "('read_log_evidence','read_metric_evidence','read_change_evidence','"
    "read_log_runbook','read_metric_runbook','read_change_runbook')) "
    "= (tool_request IS NOT NULL)) AND (tool_request IS "
    "NULL OR (jsonb_typeof(tool_request) = 'object' AND "
    "octet_length(tool_request::text) <= 8192)) AND "
    "(call_result IS NULL OR (status <> 'reserved' AND "
    "jsonb_typeof(call_result) = 'object' AND "
    "octet_length(call_result::text) <= 65536 AND "
    "COALESCE(call_result ->> 'kind', '') = kind AND "
    "((kind = 'model' AND ((operation = 'coordinate' AND "
    "COALESCE(model_input ->> 'version', '') IN "
    "('coordination-model-input-v1','coordination-experience-input-v1') "
    "AND COALESCE(call_result ->> 'version', '') = "
    "'coordination-model-result-v1') OR (operation = "
    "'synthesize' AND NOT (model_input ? 'version') AND "
    "COALESCE(call_result ->> 'version', '') = "
    "'open-hypothesis-synthesis-result-v1') OR (operation "
    "IN ('review', 'human_review') AND NOT (model_input ? "
    "'version') AND COALESCE(call_result ->> 'version', "
    "'') = 'hypothesis-review-result-v1') OR (operation "
    "NOT IN ('coordinate', 'synthesize', 'review', "
    "'human_review') AND COALESCE(model_input ->> "
    "'version', '') IN "
    "('evidence-loop-input-v1','runbook-loop-input-v1')))) "
    "OR (kind = 'tool' AND tool_request IS NOT NULL))))",
    "ck_call_reservations_invocation_index": "invocation_index BETWEEN 0 AND 99 AND (operation "
    "<> 'coordinate' OR invocation_index = 0) AND "
    "(operation <> 'review' OR invocation_index IN (0, "
    "1, 2)) AND (operation <> 'human_review' OR "
    "invocation_index = 0)",
    "ck_call_reservations_invocation_key": "CASE WHEN operation='human_review' THEN "
    "idempotency_key='human-review:' || "
    "human_review_attempt_id::text ELSE idempotency_key "
    "= investigation_attempt_id::text || ':' || "
    "COALESCE(task_attempt_id::text, 'root') || ':' || "
    "operation || CASE WHEN invocation_index = 0 THEN '' "
    "ELSE ':' || invocation_index::text END END",
    "ck_call_reservations_operation": "(operation IN ('query_logs', 'query_metrics', "
    "'list_changes', 'read_log_evidence', "
    "'read_metric_evidence', 'read_change_evidence', "
    "'read_log_runbook', 'read_metric_runbook', "
    "'read_change_runbook') AND kind = 'tool') OR (operation "
    "IN ('coordinate', 'analyze_log', 'analyze_metric', "
    "'analyze_change', 'synthesize', 'review', "
    "'human_review') AND kind = 'model')",
    "ck_call_reservations_scope": "(operation IN ('coordinate', 'synthesize', 'review', "
    "'human_review') AND task_id IS NULL AND task_attempt_id IS "
    "NULL) OR (operation NOT IN ('coordinate', 'synthesize', "
    "'review', 'human_review') AND task_id IS NOT NULL AND "
    "task_attempt_id IS NOT NULL)",
}


def _constraints(values: dict[str, str]) -> None:
    for name, expression in values.items():
        op.execute(f"ALTER TABLE investigation_call_reservations DROP CONSTRAINT {name}")
        op.execute(
            f"ALTER TABLE investigation_call_reservations "
            f"ADD CONSTRAINT {name} CHECK ({expression})"
        )


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(
        "ALTER TABLE investigation_call_reservations ADD COLUMN human_review_attempt_id UUID"
    )
    _constraints(_NEW_CONSTRAINTS)
    op.execute("""
      ALTER TABLE investigation_call_reservations
        ADD CONSTRAINT ck_call_reservations_human_owner CHECK
          ((operation='human_review') = (human_review_attempt_id IS NOT NULL)),
        ADD CONSTRAINT fk_call_reservations_human_attempt
          FOREIGN KEY (human_review_attempt_id,investigation_run_id)
          REFERENCES investigation_human_review_attempts(id,investigation_run_id)
          ON DELETE RESTRICT;
    """)
    op.execute(_GUARDS)


_TABLES = """
CREATE TABLE investigation_human_review_jobs (
id UUID NOT NULL,
investigation_run_id UUID NOT NULL,
preparation_job_id UUID NOT NULL,
wait_version INTEGER NOT NULL,
policy_version VARCHAR(40) NOT NULL,
preparation_sha256 VARCHAR(64) NOT NULL,
requested_by_id VARCHAR(100) NOT NULL,
authorization_id UUID NOT NULL,
idempotency_key VARCHAR(100) NOT NULL,
command_sha256 VARCHAR(64) NOT NULL,
status VARCHAR(16) NOT NULL,
attempt_count INTEGER NOT NULL,
available_at TIMESTAMP WITH TIME ZONE NOT NULL,
locked_by VARCHAR(100),
lease_token UUID,
locked_until TIMESTAMP WITH TIME ZONE,
last_error_code VARCHAR(100),
created_at TIMESTAMP WITH TIME ZONE NOT NULL,
updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
completed_at TIMESTAMP WITH TIME ZONE,
PRIMARY KEY (id),
CONSTRAINT uq_human_review_jobs_preparation UNIQUE (preparation_job_id),
CONSTRAINT uq_human_review_jobs_id_run UNIQUE (id, investigation_run_id),
CONSTRAINT uq_human_review_jobs_key UNIQUE (investigation_run_id, requested_by_id,
idempotency_key),
CONSTRAINT uq_human_review_jobs_authorization UNIQUE (authorization_id),
CONSTRAINT ck_human_review_jobs_version CHECK (wait_version BETWEEN 2 AND 16),
CONSTRAINT ck_human_review_jobs_policy CHECK (policy_version='human-input-reassessment-v1'),
CONSTRAINT ck_human_review_jobs_hashes CHECK (preparation_sha256 ~ '^[0-9a-f]{64}$' AND
command_sha256 ~ '^[0-9a-f]{64}$'),
CONSTRAINT ck_human_review_jobs_subject CHECK (char_length(btrim(requested_by_id)) BETWEEN 1
AND 100),
CONSTRAINT ck_human_review_jobs_key CHECK (char_length(idempotency_key) BETWEEN 8 AND 100 AND
idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'),
CONSTRAINT ck_human_review_jobs_status CHECK (status IN
('queued','running','succeeded','failed')),
CONSTRAINT ck_human_review_jobs_attempts CHECK (attempt_count BETWEEN 0 AND 3),
CONSTRAINT ck_human_review_jobs_lease CHECK ((status='running' AND locked_by IS NOT NULL AND
lease_token IS NOT NULL AND locked_until IS NOT NULL) OR (status<>'running' AND locked_by IS
NULL AND lease_token IS NULL AND locked_until IS NULL)),
CONSTRAINT ck_human_review_jobs_completion CHECK ((status IN ('queued','running') AND
completed_at IS NULL) OR (status IN ('succeeded','failed') AND completed_at IS NOT NULL)),
CONSTRAINT ck_human_review_jobs_error CHECK (status<>'failed' OR last_error_code IS NOT NULL),
CONSTRAINT ck_human_review_jobs_time CHECK (created_at<=updated_at AND
available_at>=created_at AND (completed_at IS NULL OR completed_at>=created_at)),
FOREIGN KEY(preparation_job_id) REFERENCES investigation_human_preparation_jobs (id) ON DELETE
RESTRICT,
FOREIGN KEY(authorization_id) REFERENCES operator_authorizations (id) ON DELETE RESTRICT
);

CREATE INDEX ix_human_review_jobs_claim ON investigation_human_review_jobs (status,
available_at, created_at);

CREATE TABLE investigation_human_review_attempts (
id UUID NOT NULL,
job_id UUID NOT NULL,
investigation_run_id UUID NOT NULL,
number INTEGER NOT NULL,
lease_token UUID NOT NULL,
worker_id VARCHAR(100) NOT NULL,
status VARCHAR(16) NOT NULL,
started_at TIMESTAMP WITH TIME ZONE NOT NULL,
lease_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
finished_at TIMESTAMP WITH TIME ZONE,
error_code VARCHAR(100),
PRIMARY KEY (id),
CONSTRAINT uq_human_review_attempts_id_run UNIQUE (id, investigation_run_id),
CONSTRAINT uq_human_review_attempts_scope UNIQUE (id, job_id, investigation_run_id),
CONSTRAINT fk_human_review_attempts_job FOREIGN KEY(job_id, investigation_run_id) REFERENCES
investigation_human_review_jobs (id, investigation_run_id) ON DELETE RESTRICT,
CONSTRAINT uq_human_review_attempts_number UNIQUE (job_id, number),
CONSTRAINT uq_human_review_attempts_id_job UNIQUE (id, job_id),
CONSTRAINT uq_human_review_attempts_token UNIQUE (lease_token),
CONSTRAINT ck_human_review_attempts_number CHECK (number BETWEEN 1 AND 3),
CONSTRAINT ck_human_review_attempts_status CHECK (status IN
('running','succeeded','failed','lease_expired')),
CONSTRAINT ck_human_review_attempts_completion CHECK ((status='running' AND finished_at IS
NULL AND error_code IS NULL) OR (status='succeeded' AND finished_at IS NOT NULL AND error_code
IS NULL) OR (status IN ('failed','lease_expired') AND finished_at IS NOT NULL AND error_code
IS NOT NULL)),
CONSTRAINT ck_human_review_attempts_time CHECK (lease_expires_at>started_at AND (finished_at
IS NULL OR finished_at>=started_at))
);

CREATE TABLE investigation_human_review_results (
job_id UUID NOT NULL,
attempt_id UUID NOT NULL,
source_call_id UUID NOT NULL,
content JSONB NOT NULL,
content_sha256 VARCHAR(64) NOT NULL,
created_at TIMESTAMP WITH TIME ZONE NOT NULL,
PRIMARY KEY (job_id),
CONSTRAINT fk_human_review_results_attempt FOREIGN KEY(attempt_id, job_id) REFERENCES
investigation_human_review_attempts (id, job_id) ON DELETE RESTRICT,
CONSTRAINT ck_human_review_results_content CHECK (jsonb_typeof(content)='object' AND
octet_length(content::text)<=65536),
CONSTRAINT ck_human_review_results_hash CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
FOREIGN KEY(job_id) REFERENCES investigation_human_review_jobs (id) ON DELETE RESTRICT,
FOREIGN KEY(source_call_id) REFERENCES investigation_call_reservations (id) ON DELETE RESTRICT
);

CREATE TABLE investigation_human_review_commands (
cursor BIGINT GENERATED ALWAYS AS IDENTITY,
authorization_id UUID NOT NULL,
investigation_run_id UUID NOT NULL,
preparation_job_id UUID NOT NULL,
subject_id VARCHAR(100) NOT NULL,
command JSONB NOT NULL,
raw_body TEXT NOT NULL,
command_sha256 VARCHAR(64) NOT NULL,
outcome VARCHAR(16) NOT NULL,
http_status INTEGER NOT NULL,
job_id UUID,
error_code VARCHAR(100),
error_message VARCHAR(1000),
created_at TIMESTAMP WITH TIME ZONE NOT NULL,
PRIMARY KEY (cursor),
CONSTRAINT uq_human_review_commands_authorization UNIQUE (authorization_id),
CONSTRAINT ck_human_review_commands_body CHECK (jsonb_typeof(command)='object' AND
octet_length(command::text)<=32768),
CONSTRAINT ck_human_review_commands_raw CHECK (octet_length(raw_body) BETWEEN 2 AND 65536),
CONSTRAINT ck_human_review_commands_hash CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
CONSTRAINT ck_human_review_commands_subject CHECK (char_length(btrim(subject_id)) BETWEEN 1
AND 100),
CONSTRAINT ck_human_review_commands_outcome CHECK ((outcome IN ('applied','replayed') AND
job_id IS NOT NULL AND error_code IS NULL AND error_message IS NULL AND ((outcome='applied'
AND http_status=201) OR (outcome='replayed' AND http_status=200))) OR (outcome='error' AND
job_id IS NULL AND error_code IS NOT NULL AND error_message IS NOT NULL AND http_status IN
(404,409))),
CONSTRAINT ck_human_review_commands_error CHECK (error_code IS NULL OR error_code ~
'^[a-z0-9_]{1,100}$'),
FOREIGN KEY(authorization_id) REFERENCES operator_authorizations (id) ON DELETE RESTRICT,
FOREIGN KEY(job_id) REFERENCES investigation_human_review_jobs (id) ON DELETE RESTRICT
);

CREATE INDEX ix_human_review_commands_run_cursor ON investigation_human_review_commands
(investigation_run_id, cursor);
"""

_GUARDS = """
CREATE FUNCTION guard_human_review_job() RETURNS trigger AS $$
DECLARE
  preparation investigation_human_preparation_jobs%ROWTYPE;
  artifact investigation_human_preparation_artifacts%ROWTYPE;
  admission operator_authorizations%ROWTYPE;
  run investigation_runs%ROWTYPE;
  current_version INTEGER;
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION '重评任务不可删除'; END IF;
  PERFORM 1 FROM work_items WHERE investigation_run_id=NEW.investigation_run_id FOR UPDATE;
  IF TG_OP='UPDATE' THEN
    IF ROW(NEW.id,NEW.investigation_run_id,NEW.preparation_job_id,NEW.preparation_sha256,
        NEW.wait_version,NEW.policy_version,NEW.requested_by_id,NEW.authorization_id,
        NEW.idempotency_key,NEW.command_sha256,NEW.created_at) IS DISTINCT FROM
       ROW(OLD.id,OLD.investigation_run_id,OLD.preparation_job_id,OLD.preparation_sha256,
        OLD.wait_version,OLD.policy_version,OLD.requested_by_id,OLD.authorization_id,
        OLD.idempotency_key,OLD.command_sha256,OLD.created_at)
       OR (OLD.status IN ('succeeded','failed') AND NEW IS DISTINCT FROM OLD) THEN
      RAISE EXCEPTION '重评任务固定来源或终态不可改写';
    END IF;
    RETURN NEW;
  END IF;
  SELECT * INTO preparation FROM investigation_human_preparation_jobs
    WHERE id=NEW.preparation_job_id;
  SELECT * INTO artifact FROM investigation_human_preparation_artifacts
    WHERE job_id=NEW.preparation_job_id;
  SELECT * INTO run FROM investigation_runs WHERE id=NEW.investigation_run_id;
  SELECT max(state_version) INTO current_version FROM investigation_human_wait_events
    WHERE investigation_run_id=NEW.investigation_run_id;
  IF preparation.id IS NULL OR preparation.status<>'succeeded' OR artifact.job_id IS NULL
    OR preparation.investigation_run_id IS DISTINCT FROM NEW.investigation_run_id
    OR preparation.wait_version IS DISTINCT FROM NEW.wait_version
    OR artifact.content_sha256 IS DISTINCT FROM NEW.preparation_sha256
    OR run.status IS DISTINCT FROM 'await_human' OR current_version<>NEW.wait_version
    OR run.workflow_version<>'v3' OR run.data_source_mode<>'frozen'
    OR run.graph_version IS DISTINCT FROM 'dynamic-investigation-graph-v15'
    OR NEW.created_at<artifact.created_at OR NEW.status<>'queued' OR NEW.attempt_count<>0 THEN
    RAISE EXCEPTION '重评任务需要当前人工等待的精确成功准备工件';
  END IF;
  SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
  IF admission.id IS NULL OR admission.policy_version<>'operator-auth-v1'
    OR admission.role<>'engineer' OR NOT admission.permissions ? 'manage_incident'
    OR admission.subject_id IS DISTINCT FROM NEW.requested_by_id OR admission.method<>'POST'
    OR admission.operation<>'reassess_investigation_human_input'
    OR admission.route<>'/api/v1/investigations/{run_id}/human-wait/preparations/' ||
                         '{preparation_id}/reassessments'
    OR admission.resources IS DISTINCT FROM jsonb_build_object(
       'run_id',NEW.investigation_run_id::text,'preparation_id',NEW.preparation_job_id::text)
    OR admission.authorized_at>NEW.created_at THEN
    RAISE EXCEPTION '重评任务缺少同源可信工程师准入';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER human_review_jobs_guard BEFORE INSERT OR UPDATE OR DELETE
  ON investigation_human_review_jobs FOR EACH ROW EXECUTE FUNCTION guard_human_review_job();

CREATE FUNCTION guard_human_review_attempt() RETURNS trigger AS $$
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION '重评尝试不可删除'; END IF;
  IF TG_OP='UPDATE' AND
     (ROW(NEW.id,NEW.job_id,NEW.investigation_run_id,NEW.number,NEW.lease_token,
          NEW.worker_id,NEW.started_at,NEW.lease_expires_at) IS DISTINCT FROM
      ROW(OLD.id,OLD.job_id,OLD.investigation_run_id,OLD.number,OLD.lease_token,
          OLD.worker_id,OLD.started_at,OLD.lease_expires_at) OR OLD.status<>'running') THEN
    RAISE EXCEPTION '重评尝试身份或终态不可改写';
  END IF;
  IF TG_OP='INSERT' AND NEW.status<>'running' THEN
    RAISE EXCEPTION '重评尝试必须从运行状态开始';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER human_review_attempts_guard BEFORE INSERT OR UPDATE OR DELETE
  ON investigation_human_review_attempts FOR EACH ROW EXECUTE FUNCTION guard_human_review_attempt();

CREATE FUNCTION guard_human_review_command() RETURNS trigger AS $$
DECLARE
  admission operator_authorizations%ROWTYPE;
  job investigation_human_review_jobs%ROWTYPE;
  preparation investigation_human_preparation_jobs%ROWTYPE;
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '重评命令审计不可更新或删除'; END IF;
  SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
  IF admission.id IS NULL OR admission.policy_version<>'operator-auth-v1'
    OR admission.role<>'engineer' OR NOT admission.permissions ? 'manage_incident'
    OR admission.subject_id IS DISTINCT FROM NEW.subject_id OR admission.method<>'POST'
    OR admission.operation<>'reassess_investigation_human_input'
    OR admission.route<>'/api/v1/investigations/{run_id}/human-wait/preparations/' ||
                         '{preparation_id}/reassessments'
    OR admission.resources IS DISTINCT FROM jsonb_build_object(
       'run_id',NEW.investigation_run_id::text,'preparation_id',NEW.preparation_job_id::text)
    OR admission.authorized_at>NEW.created_at OR admission.body_sha256 IS DISTINCT FROM
       encode(sha256(convert_to(NEW.raw_body,'UTF8')),'hex') THEN
    RAISE EXCEPTION '重评命令缺少精确原始正文或同源可信准入';
  END IF;
  IF NEW.command->>'action' IS DISTINCT FROM 'reassess'
    OR NEW.command->>'handoff_id' IS NULL
    OR COALESCE(NEW.command->>'source_sha256','') !~ '^[0-9a-f]{64}$'
    OR COALESCE(NEW.command->>'preparation_sha256','') !~ '^[0-9a-f]{64}$'
    OR jsonb_typeof(NEW.command->'expected_wait_version') IS DISTINCT FROM 'number'
    OR NEW.command->'expected_wait_version' NOT IN
       (SELECT to_jsonb(value) FROM generate_series(1,17) value)
    OR COALESCE(char_length(NEW.command->>'idempotency_key'),0) NOT BETWEEN 8 AND 100
    OR (NEW.command->>'idempotency_key') !~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'
    OR COALESCE(char_length(btrim(NEW.command->>'reason')),0) NOT BETWEEN 1 AND 1000 THEN
    RAISE EXCEPTION '重评命令缺少有界精确来源绑定';
  END IF;
  IF NEW.outcome IN ('applied','replayed') THEN
    SELECT * INTO job FROM investigation_human_review_jobs WHERE id=NEW.job_id;
    SELECT * INTO preparation FROM investigation_human_preparation_jobs
      WHERE id=NEW.preparation_job_id;
    IF job.id IS NULL OR job.investigation_run_id IS DISTINCT FROM NEW.investigation_run_id
      OR job.preparation_job_id IS DISTINCT FROM NEW.preparation_job_id
      OR job.requested_by_id IS DISTINCT FROM NEW.subject_id
      OR job.command_sha256 IS DISTINCT FROM NEW.command_sha256
      OR job.idempotency_key IS DISTINCT FROM NEW.command->>'idempotency_key'
      OR job.preparation_sha256 IS DISTINCT FROM NEW.command->>'preparation_sha256'
      OR preparation.handoff_id::text IS DISTINCT FROM NEW.command->>'handoff_id'
      OR preparation.source #>> '{handoff,source_sha256}'
         IS DISTINCT FROM NEW.command->>'source_sha256'
      OR to_jsonb(job.wait_version) IS DISTINCT FROM NEW.command->'expected_wait_version'
      OR job.created_at>NEW.created_at
      OR (NEW.outcome='applied' AND (job.authorization_id IS DISTINCT FROM NEW.authorization_id
                                   OR job.created_at IS DISTINCT FROM NEW.created_at))
      OR (NEW.outcome='replayed' AND job.authorization_id=NEW.authorization_id) THEN
      RAISE EXCEPTION '重评命令结果与原始任务不一致';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER human_review_commands_guard BEFORE INSERT OR UPDATE OR DELETE
  ON investigation_human_review_commands FOR EACH ROW EXECUTE FUNCTION guard_human_review_command();

CREATE FUNCTION guard_human_review_call() RETURNS trigger AS $$
DECLARE
  attempt investigation_human_review_attempts%ROWTYPE;
  job investigation_human_review_jobs%ROWTYPE;
  preparation investigation_human_preparation_jobs%ROWTYPE;
  artifact investigation_human_preparation_artifacts%ROWTYPE;
  handoff investigation_terminal_handoffs%ROWTYPE;
  run investigation_runs%ROWTYPE;
  current_version INTEGER;
  model_count INTEGER;
  input_occupied NUMERIC;
  output_occupied NUMERIC;
  cost_occupied NUMERIC;
  input_price NUMERIC;
  output_price NUMERIC;
BEGIN
  IF TG_OP='DELETE' THEN
    IF OLD.operation='human_review' THEN RAISE EXCEPTION '重评调用不可删除'; END IF;
    RETURN OLD;
  END IF;
  IF NEW.operation<>'human_review' THEN
    IF TG_OP='UPDATE' AND OLD.operation='human_review' THEN
      RAISE EXCEPTION '重评调用操作不可改写';
    END IF;
    RETURN NEW;
  END IF;
  PERFORM 1 FROM work_items WHERE investigation_run_id=NEW.investigation_run_id FOR UPDATE;
  IF TG_OP='UPDATE' THEN
    IF OLD.operation<>'human_review' OR OLD.status<>'reserved'
      OR ROW(NEW.id,NEW.investigation_run_id,NEW.investigation_attempt_id,
          NEW.human_review_attempt_id,NEW.task_id,NEW.task_attempt_id,NEW.sequence,
          NEW.invocation_index,NEW.idempotency_key,NEW.operation,NEW.kind,NEW.reserved_at,
          NEW.model_input,NEW.model_allocation,NEW.context_manifest,NEW.tool_request)
        IS DISTINCT FROM
         ROW(OLD.id,OLD.investigation_run_id,OLD.investigation_attempt_id,
          OLD.human_review_attempt_id,OLD.task_id,OLD.task_attempt_id,OLD.sequence,
          OLD.invocation_index,OLD.idempotency_key,OLD.operation,OLD.kind,OLD.reserved_at,
          OLD.model_input,OLD.model_allocation,OLD.context_manifest,OLD.tool_request)
      OR NEW.status='reserved' THEN
      RAISE EXCEPTION '重评调用只允许预留到一次不可变终态';
    END IF;
  END IF;
  SELECT * INTO attempt FROM investigation_human_review_attempts
    WHERE id=NEW.human_review_attempt_id;
  SELECT * INTO job FROM investigation_human_review_jobs WHERE id=attempt.job_id;
  SELECT * INTO preparation FROM investigation_human_preparation_jobs
    WHERE id=job.preparation_job_id;
  SELECT * INTO artifact FROM investigation_human_preparation_artifacts
    WHERE job_id=job.preparation_job_id;
  SELECT * INTO handoff FROM investigation_terminal_handoffs WHERE id=preparation.handoff_id;
  SELECT * INTO run FROM investigation_runs WHERE id=NEW.investigation_run_id;
  IF attempt.id IS NULL OR job.id IS NULL OR preparation.id IS NULL OR artifact.job_id IS NULL
    OR attempt.investigation_run_id IS DISTINCT FROM NEW.investigation_run_id
    OR job.investigation_run_id IS DISTINCT FROM NEW.investigation_run_id
    OR handoff.investigation_attempt_id IS DISTINCT FROM NEW.investigation_attempt_id
    OR preparation.investigation_run_id IS DISTINCT FROM NEW.investigation_run_id
    OR artifact.content_sha256 IS DISTINCT FROM job.preparation_sha256
    OR NEW.model_input IS DISTINCT FROM artifact.content #> '{prepared_input,model_input}'
    OR NEW.model_allocation IS DISTINCT FROM artifact.content #> '{prepared_input,allocation}'
    OR NEW.context_manifest IS DISTINCT FROM artifact.content #> '{prepared_input,manifest}'
    OR NEW.model_input IS NULL OR NEW.model_allocation IS NULL
    OR NEW.model_input='null'::jsonb OR NEW.model_allocation='null'::jsonb THEN
    RAISE EXCEPTION '重评调用缺少真实执行归属或精确准备输入';
  END IF;
  -- 失联收尾不授予执行资格，允许已取消或过期旧尝试仅结算 unknown。
  IF TG_OP='UPDATE' AND NEW.status='unknown' THEN
    IF NEW.call_result IS NOT NULL THEN
      RAISE EXCEPTION '未知重评调用不能保存成功意见';
    END IF;
    RETURN NEW;
  END IF;
  IF attempt.status<>'running' OR job.status<>'running' OR attempt.number<>job.attempt_count
    OR attempt.lease_token IS DISTINCT FROM job.lease_token
    OR attempt.worker_id IS DISTINCT FROM job.locked_by
    OR attempt.lease_expires_at IS DISTINCT FROM job.locked_until
    OR NEW.reserved_at<attempt.started_at OR NEW.reserved_at>=attempt.lease_expires_at
    OR (TG_OP='UPDATE' AND NEW.finished_at>=attempt.lease_expires_at) THEN
    RAISE EXCEPTION '重评调用需要当前有效新阶段租约';
  END IF;
  IF TG_OP='UPDATE' AND NEW.status='failed' THEN
    IF NEW.call_result IS NOT NULL THEN RAISE EXCEPTION '失败重评调用不能保存意见'; END IF;
    RETURN NEW;
  END IF;
  SELECT max(state_version) INTO current_version FROM investigation_human_wait_events
    WHERE investigation_run_id=NEW.investigation_run_id;
  IF run.status IS DISTINCT FROM 'await_human' OR current_version IS DISTINCT FROM job.wait_version
    OR run.started_at IS NULL OR run.call_budget IS NULL OR run.model_budget IS NULL THEN
    RAISE EXCEPTION '重评调用不能推进已取消或变化的人工等待';
  END IF;
  IF TG_OP='UPDATE' THEN
    IF NEW.status<>'succeeded' OR NEW.call_result IS NULL
      OR NEW.finished_at>=run.started_at +
         make_interval(secs=>(run.call_budget->>'max_duration_seconds')::int) THEN
      RAISE EXCEPTION '重评成功调用必须保存结构化意见';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.status<>'reserved' OR NEW.reserved_at>=run.started_at +
       make_interval(secs=>(run.call_budget->>'max_duration_seconds')::int) THEN
    RAISE EXCEPTION '重评预留不能跳过原运行时限';
  END IF;
  SELECT count(*) INTO model_count FROM investigation_call_reservations
    WHERE investigation_run_id=NEW.investigation_run_id AND kind='model';
  IF model_count>=(run.call_budget->>'max_model_calls')::int
    OR NEW.sequence<>(SELECT count(*)+1 FROM investigation_call_reservations
                     WHERE investigation_run_id=NEW.investigation_run_id)
    OR EXISTS (SELECT 1 FROM investigation_call_reservations
               WHERE investigation_run_id=NEW.investigation_run_id AND status='reserved') THEN
    RAISE EXCEPTION '重评预留超过原调用次数或存在未结算调用';
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
    WHERE investigation_run_id=NEW.investigation_run_id AND kind='model';
  IF input_occupied+(NEW.model_allocation->>'estimated_input_tokens')::numeric>
       (run.model_budget->>'max_input_tokens')::numeric
    OR output_occupied+(NEW.model_allocation->>'output_token_limit')::numeric>
       (run.model_budget->>'max_output_tokens')::numeric
    OR cost_occupied+(NEW.model_allocation->>'reserved_cost_usd')::numeric>
       (run.model_budget->>'max_estimated_cost_usd')::numeric THEN
    RAISE EXCEPTION '重评预留超过原累计模型预算';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER human_review_calls_guard BEFORE INSERT OR UPDATE OR DELETE
  ON investigation_call_reservations FOR EACH ROW EXECUTE FUNCTION guard_human_review_call();

CREATE FUNCTION guard_human_review_result() RETURNS trigger AS $$
DECLARE
  job investigation_human_review_jobs%ROWTYPE;
  attempt investigation_human_review_attempts%ROWTYPE;
  caller investigation_human_review_attempts%ROWTYPE;
  source_call investigation_call_reservations%ROWTYPE;
  run investigation_runs%ROWTYPE;
  current_version INTEGER;
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '重评意见不可更新或删除'; END IF;
  SELECT * INTO job FROM investigation_human_review_jobs WHERE id=NEW.job_id;
  IF job.id IS NULL THEN RAISE EXCEPTION '重评意见缺少任务'; END IF;
  PERFORM 1 FROM work_items WHERE investigation_run_id=job.investigation_run_id FOR UPDATE;
  SELECT * INTO job FROM investigation_human_review_jobs WHERE id=NEW.job_id FOR UPDATE;
  SELECT * INTO attempt FROM investigation_human_review_attempts WHERE id=NEW.attempt_id;
  SELECT * INTO source_call FROM investigation_call_reservations WHERE id=NEW.source_call_id;
  SELECT * INTO caller FROM investigation_human_review_attempts
    WHERE id=source_call.human_review_attempt_id;
  SELECT * INTO run FROM investigation_runs WHERE id=job.investigation_run_id;
  SELECT max(state_version) INTO current_version FROM investigation_human_wait_events
    WHERE investigation_run_id=job.investigation_run_id;
  IF job.status<>'running' OR attempt.id IS NULL OR attempt.job_id IS DISTINCT FROM job.id
    OR attempt.status<>'running' OR attempt.number<>job.attempt_count
    OR attempt.lease_token IS DISTINCT FROM job.lease_token
    OR attempt.lease_expires_at IS DISTINCT FROM job.locked_until
    OR NEW.created_at<attempt.started_at OR NEW.created_at>=attempt.lease_expires_at
    OR source_call.id IS NULL OR source_call.operation<>'human_review'
    OR source_call.status<>'succeeded' OR caller.job_id IS DISTINCT FROM job.id
    OR source_call.call_result IS DISTINCT FROM NEW.content
    OR source_call.finished_at>NEW.created_at
    OR EXISTS (SELECT 1 FROM investigation_call_reservations c
        WHERE c.investigation_run_id=job.investigation_run_id AND c.kind='model'
        AND ((c.model_usage->>'input_tokens')::numeric>
               (c.model_allocation->>'estimated_input_tokens')::numeric
          OR (c.model_usage->>'output_tokens')::numeric>
               (c.model_allocation->>'output_token_limit')::numeric))
    OR run.status IS DISTINCT FROM 'await_human'
    OR current_version IS DISTINCT FROM job.wait_version
    OR NEW.created_at>=run.started_at +
       make_interval(secs=>(run.call_budget->>'max_duration_seconds')::int) THEN
    RAISE EXCEPTION '重评意见需要当前租约及同任务成功调用';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER human_review_results_guard BEFORE INSERT OR UPDATE OR DELETE
  ON investigation_human_review_results FOR EACH ROW EXECUTE FUNCTION guard_human_review_result();

CREATE FUNCTION validate_human_review_state() RETURNS trigger AS $$
DECLARE
  target UUID;
  job investigation_human_review_jobs%ROWTYPE;
  attempt investigation_human_review_attempts%ROWTYPE;
  result investigation_human_review_results%ROWTYPE;
  audit investigation_human_review_commands%ROWTYPE;
  attempts INTEGER;
BEGIN
  IF TG_TABLE_NAME='investigation_human_review_jobs' THEN target:=NEW.id;
  ELSE target:=NEW.job_id; END IF;
  SELECT * INTO job FROM investigation_human_review_jobs WHERE id=target;
  SELECT * INTO audit FROM investigation_human_review_commands
    WHERE authorization_id=job.authorization_id;
  SELECT count(*) INTO attempts FROM investigation_human_review_attempts WHERE job_id=job.id;
  SELECT * INTO attempt FROM investigation_human_review_attempts
    WHERE job_id=job.id ORDER BY number DESC LIMIT 1;
  SELECT * INTO result FROM investigation_human_review_results WHERE job_id=job.id;
  IF job.id IS NULL OR audit.authorization_id IS NULL OR audit.outcome<>'applied'
    OR audit.job_id IS DISTINCT FROM job.id OR audit.created_at IS DISTINCT FROM job.created_at
    OR audit.subject_id IS DISTINCT FROM job.requested_by_id
    OR audit.command_sha256 IS DISTINCT FROM job.command_sha256
    OR attempts<>job.attempt_count
    OR (attempt.id IS NOT NULL AND attempt.number<>job.attempt_count)
    OR EXISTS (SELECT 1 FROM investigation_human_review_attempts a WHERE a.job_id=job.id
               AND a.number<job.attempt_count AND a.status IN ('running','succeeded')) THEN
    RAISE EXCEPTION '重评任务缺少同源成功命令或连续尝试';
  END IF;
  IF job.status='running' THEN
    IF attempt.id IS NULL OR attempt.status<>'running'
      OR attempt.lease_token IS DISTINCT FROM job.lease_token
      OR attempt.worker_id IS DISTINCT FROM job.locked_by
      OR attempt.lease_expires_at IS DISTINCT FROM job.locked_until
      OR result.job_id IS NOT NULL THEN
      RAISE EXCEPTION '重评任务与当前租约尝试不一致';
    END IF;
  ELSIF job.status='succeeded' THEN
    IF attempt.id IS NULL OR attempt.status<>'succeeded' OR result.job_id IS NULL
      OR result.attempt_id IS DISTINCT FROM attempt.id
      OR result.created_at IS DISTINCT FROM job.completed_at
      OR attempt.finished_at IS DISTINCT FROM result.created_at THEN
      RAISE EXCEPTION '重评成功需要同尝试和同时间的独立意见';
    END IF;
  ELSIF result.job_id IS NOT NULL OR attempt.status IN ('running','succeeded') THEN
    RAISE EXCEPTION '未成功重评不能保留活动尝试或意见';
  END IF;
  IF job.status<>'running' AND EXISTS (
     SELECT 1 FROM investigation_call_reservations c
       JOIN investigation_human_review_attempts a ON a.id=c.human_review_attempt_id
       WHERE a.job_id=job.id AND c.status='reserved') THEN
    RAISE EXCEPTION '重评任务收尾不能遗留未结算预留';
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER human_review_jobs_state AFTER INSERT OR UPDATE
  ON investigation_human_review_jobs DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION validate_human_review_state();
CREATE CONSTRAINT TRIGGER human_review_attempts_state AFTER INSERT OR UPDATE
  ON investigation_human_review_attempts DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION validate_human_review_state();
CREATE CONSTRAINT TRIGGER human_review_results_state AFTER INSERT
  ON investigation_human_review_results DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION validate_human_review_state();
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_human_review_jobs)
          OR EXISTS (SELECT 1 FROM investigation_human_review_attempts)
          OR EXISTS (SELECT 1 FROM investigation_human_review_results)
          OR EXISTS (SELECT 1 FROM investigation_human_review_commands)
          OR EXISTS (SELECT 1 FROM investigation_call_reservations
                     WHERE operation='human_review' OR human_review_attempt_id IS NOT NULL) THEN
          RAISE EXCEPTION '不能删除已保存的人工重评事实或预算调用';
        END IF;
      END $$;
      DROP TRIGGER human_review_calls_guard ON investigation_call_reservations;
      DROP FUNCTION guard_human_review_call();
      ALTER TABLE investigation_call_reservations
        DROP CONSTRAINT fk_call_reservations_human_attempt,
        DROP CONSTRAINT ck_call_reservations_human_owner;
    """)
    _constraints(_OLD_CONSTRAINTS)
    op.execute("""
      ALTER TABLE investigation_call_reservations DROP COLUMN human_review_attempt_id;
      DROP TRIGGER human_review_jobs_state ON investigation_human_review_jobs;
      DROP TRIGGER human_review_attempts_state ON investigation_human_review_attempts;
      DROP TRIGGER human_review_results_state ON investigation_human_review_results;
      DROP FUNCTION validate_human_review_state();
      DROP TABLE investigation_human_review_commands;
      DROP TABLE investigation_human_review_results;
      DROP TABLE investigation_human_review_attempts;
      DROP TABLE investigation_human_review_jobs;
      DROP FUNCTION guard_human_review_command();
      DROP FUNCTION guard_human_review_result();
      DROP FUNCTION guard_human_review_attempt();
      DROP FUNCTION guard_human_review_job();
    """)
