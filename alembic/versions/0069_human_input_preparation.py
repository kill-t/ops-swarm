"""新增可信人工输入准备任务与独立租约工件，保留原调查事实。"""

from alembic import op

revision = "0069_human_input_preparation"
down_revision = "0068_human_wait_commands"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(_GUARDS)


_TABLES = """
CREATE TABLE investigation_human_preparation_jobs (
id UUID NOT NULL,
investigation_run_id UUID NOT NULL,
handoff_id UUID NOT NULL,
wait_version INTEGER NOT NULL,
policy_version VARCHAR(40) NOT NULL,
source JSONB NOT NULL,
source_sha256 VARCHAR(64) NOT NULL,
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
CONSTRAINT uq_human_preparation_jobs_version UNIQUE (investigation_run_id, wait_version),
CONSTRAINT uq_human_preparation_jobs_key UNIQUE (investigation_run_id, requested_by_id,
idempotency_key),
CONSTRAINT uq_human_preparation_jobs_authorization UNIQUE (authorization_id),
CONSTRAINT fk_human_preparation_jobs_handoff FOREIGN KEY(handoff_id, investigation_run_id)
REFERENCES investigation_terminal_handoffs (id, investigation_run_id) ON DELETE RESTRICT,
CONSTRAINT ck_human_preparation_jobs_version CHECK (wait_version BETWEEN 2 AND 16),
CONSTRAINT ck_human_preparation_jobs_policy CHECK
(policy_version='human-input-preparation-v1'),
CONSTRAINT ck_human_preparation_jobs_source CHECK (jsonb_typeof(source)='object' AND
octet_length(source::text)<=524288),
CONSTRAINT ck_human_preparation_jobs_hashes CHECK (source_sha256 ~ '^[0-9a-f]{64}$' AND
command_sha256 ~ '^[0-9a-f]{64}$'),
CONSTRAINT ck_human_preparation_jobs_subject CHECK (char_length(btrim(requested_by_id))
BETWEEN 1 AND 100),
CONSTRAINT ck_human_preparation_jobs_key CHECK (char_length(idempotency_key) BETWEEN 8 AND 100
AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'),
CONSTRAINT ck_human_preparation_jobs_status CHECK (status IN
('queued','running','succeeded','failed')),
CONSTRAINT ck_human_preparation_jobs_attempts CHECK (attempt_count BETWEEN 0 AND 3),
CONSTRAINT ck_human_preparation_jobs_lease CHECK ((status='running' AND locked_by IS NOT NULL
AND lease_token IS NOT NULL AND locked_until IS NOT NULL) OR (status<>'running' AND locked_by
IS NULL AND lease_token IS NULL AND locked_until IS NULL)),
CONSTRAINT ck_human_preparation_jobs_completion CHECK ((status IN ('queued','running') AND
completed_at IS NULL) OR (status IN ('succeeded','failed') AND completed_at IS NOT NULL)),
CONSTRAINT ck_human_preparation_jobs_error CHECK (status<>'failed' OR last_error_code IS NOT
NULL),
CONSTRAINT ck_human_preparation_jobs_time CHECK (created_at<=updated_at AND
available_at>=created_at AND (completed_at IS NULL OR completed_at>=created_at)),
FOREIGN KEY(authorization_id) REFERENCES operator_authorizations (id) ON DELETE RESTRICT
);

CREATE INDEX ix_human_preparation_jobs_claim ON investigation_human_preparation_jobs (status,
available_at, created_at);

CREATE TABLE investigation_human_preparation_attempts (
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
CONSTRAINT uq_human_preparation_attempts_number UNIQUE (job_id, number),
CONSTRAINT uq_human_preparation_attempts_id_job UNIQUE (id, job_id),
CONSTRAINT uq_human_preparation_attempts_token UNIQUE (lease_token),
CONSTRAINT ck_human_preparation_attempts_number CHECK (number BETWEEN 1 AND 3),
CONSTRAINT ck_human_preparation_attempts_status CHECK (status IN
('running','succeeded','failed','lease_expired')),
CONSTRAINT ck_human_preparation_attempts_completion CHECK ((status='running' AND finished_at
IS NULL AND error_code IS NULL) OR (status='succeeded' AND finished_at IS NOT NULL AND
error_code IS NULL) OR (status IN ('failed','lease_expired') AND finished_at IS NOT NULL AND
error_code IS NOT NULL)),
CONSTRAINT ck_human_preparation_attempts_time CHECK (lease_expires_at>started_at AND
(finished_at IS NULL OR finished_at>=started_at)),
FOREIGN KEY(job_id) REFERENCES investigation_human_preparation_jobs (id) ON DELETE RESTRICT
);

CREATE TABLE investigation_human_preparation_artifacts (
job_id UUID NOT NULL,
attempt_id UUID NOT NULL,
version VARCHAR(40) NOT NULL,
content JSONB NOT NULL,
content_sha256 VARCHAR(64) NOT NULL,
created_at TIMESTAMP WITH TIME ZONE NOT NULL,
PRIMARY KEY (job_id),
CONSTRAINT fk_human_preparation_artifacts_attempt FOREIGN KEY(attempt_id, job_id) REFERENCES
investigation_human_preparation_attempts (id, job_id) ON DELETE RESTRICT,
CONSTRAINT ck_human_preparation_artifacts_version CHECK
(version='human-preparation-artifact-v1'),
CONSTRAINT ck_human_preparation_artifacts_content CHECK (jsonb_typeof(content)='object' AND
octet_length(content::text)<=1048576),
CONSTRAINT ck_human_preparation_artifacts_hash CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
FOREIGN KEY(job_id) REFERENCES investigation_human_preparation_jobs (id) ON DELETE RESTRICT
);

CREATE TABLE investigation_human_preparation_commands (
cursor BIGINT GENERATED ALWAYS AS IDENTITY,
authorization_id UUID NOT NULL,
investigation_run_id UUID NOT NULL,
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
CONSTRAINT uq_human_preparation_commands_authorization UNIQUE (authorization_id),
CONSTRAINT ck_human_preparation_commands_body CHECK (jsonb_typeof(command)='object' AND
octet_length(command::text)<=32768),
CONSTRAINT ck_human_preparation_commands_raw CHECK (octet_length(raw_body) BETWEEN 2 AND
65536),
CONSTRAINT ck_human_preparation_commands_hash CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
CONSTRAINT ck_human_preparation_commands_subject CHECK (char_length(btrim(subject_id)) BETWEEN
1 AND 100),
CONSTRAINT ck_human_preparation_commands_outcome CHECK ((outcome IN ('applied','replayed') AND
job_id IS NOT NULL AND error_code IS NULL AND error_message IS NULL AND ((outcome='applied'
AND http_status=201) OR (outcome='replayed' AND http_status=200))) OR (outcome='error' AND
job_id IS NULL AND error_code IS NOT NULL AND error_message IS NOT NULL AND http_status IN
(404,409))),
CONSTRAINT ck_human_preparation_commands_error CHECK (error_code IS NULL OR error_code ~
'^[a-z0-9_]{1,100}$'),
FOREIGN KEY(authorization_id) REFERENCES operator_authorizations (id) ON DELETE RESTRICT,
FOREIGN KEY(job_id) REFERENCES investigation_human_preparation_jobs (id) ON DELETE RESTRICT
);

CREATE INDEX ix_human_preparation_commands_run_cursor ON
investigation_human_preparation_commands (investigation_run_id, cursor);
"""


_GUARDS = """
CREATE FUNCTION guard_human_preparation_job() RETURNS trigger AS $$
DECLARE
  run investigation_runs%ROWTYPE;
  handoff investigation_terminal_handoffs%ROWTYPE;
  admission operator_authorizations%ROWTYPE;
  work work_items%ROWTYPE;
  current_version INTEGER;
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION '准备任务事实不可删除'; END IF;
  SELECT * INTO work FROM work_items WHERE investigation_run_id=NEW.investigation_run_id
    FOR UPDATE;
  IF TG_OP='UPDATE' THEN
    IF ROW(NEW.id,NEW.investigation_run_id,NEW.handoff_id,NEW.wait_version,NEW.policy_version,
           NEW.source,NEW.source_sha256,NEW.requested_by_id,NEW.authorization_id,
           NEW.idempotency_key,NEW.command_sha256,NEW.created_at) IS DISTINCT FROM
       ROW(OLD.id,OLD.investigation_run_id,OLD.handoff_id,OLD.wait_version,OLD.policy_version,
           OLD.source,OLD.source_sha256,OLD.requested_by_id,OLD.authorization_id,
           OLD.idempotency_key,OLD.command_sha256,OLD.created_at)
       OR (OLD.status IN ('succeeded','failed') AND NEW IS DISTINCT FROM OLD) THEN
      RAISE EXCEPTION '准备任务固定来源或终态不可改写';
    END IF;
    RETURN NEW;
  END IF;
  SELECT * INTO run FROM investigation_runs WHERE id=NEW.investigation_run_id;
  SELECT * INTO handoff FROM investigation_terminal_handoffs
    WHERE id=NEW.handoff_id AND investigation_run_id=NEW.investigation_run_id;
  SELECT max(state_version) INTO current_version FROM investigation_human_wait_events
    WHERE investigation_run_id=NEW.investigation_run_id;
  IF work.id IS NULL OR work.status<>'completed' OR run.status IS DISTINCT FROM 'await_human'
    OR run.workflow_version<>'v3' OR run.data_source_mode<>'frozen'
    OR run.graph_version IS DISTINCT FROM 'dynamic-investigation-graph-v15'
    OR handoff.id IS NULL OR handoff.destination<>'await_human'
    OR current_version IS DISTINCT FROM NEW.wait_version
    OR EXISTS (SELECT 1 FROM investigation_human_wait_events
               WHERE investigation_run_id=NEW.investigation_run_id AND action<>'supplement')
    OR NEW.status<>'queued' OR NEW.attempt_count<>0
    OR NEW.created_at<handoff.created_at
    OR NEW.created_at<(SELECT max(created_at) FROM investigation_human_wait_events
                       WHERE investigation_run_id=NEW.investigation_run_id)
    OR NEW.source #>> '{handoff,id}' IS DISTINCT FROM NEW.handoff_id::text
    OR NEW.source #>> '{handoff,investigation_run_id}'
       IS DISTINCT FROM NEW.investigation_run_id::text
    OR NEW.source #>> '{handoff,source_sha256}' IS DISTINCT FROM handoff.source_sha256
    OR NEW.source #>> '{wait,investigation_run_id}'
       IS DISTINCT FROM NEW.investigation_run_id::text
    OR NEW.source #>> '{wait,handoff_id}' IS DISTINCT FROM NEW.handoff_id::text
    OR NEW.source #>> '{wait,source_sha256}' IS DISTINCT FROM handoff.source_sha256
    OR NEW.source #>> '{wait,state_version}' IS DISTINCT FROM NEW.wait_version::text
    OR NEW.source #>> '{wait,status}' IS DISTINCT FROM 'waiting'
    OR jsonb_typeof(NEW.source #> '{wait,events}') IS DISTINCT FROM 'array' THEN
    RAISE EXCEPTION '准备任务必须来自当前未取消的精确人工补充等待';
  END IF;
  IF jsonb_array_length(NEW.source #> '{wait,events}')<>NEW.wait_version-1 THEN
    RAISE EXCEPTION '准备任务必须保留完整人工补充前缀';
  END IF;
  SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
  IF admission.id IS NULL OR admission.policy_version<>'operator-auth-v1'
    OR admission.role<>'engineer' OR NOT admission.permissions ? 'manage_incident'
    OR admission.subject_id IS DISTINCT FROM NEW.requested_by_id
    OR admission.method<>'POST' OR admission.operation<>'prepare_investigation_human_input'
    OR admission.route<>'/api/v1/investigations/{run_id}/human-wait/preparations'
    OR admission.resources IS DISTINCT FROM
       jsonb_build_object('run_id',NEW.investigation_run_id::text)
    OR admission.authorized_at>NEW.created_at THEN
    RAISE EXCEPTION '准备任务缺少同源可信工程师授权';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER human_preparation_jobs_guard BEFORE INSERT OR UPDATE OR DELETE
  ON investigation_human_preparation_jobs FOR EACH ROW EXECUTE FUNCTION
  guard_human_preparation_job();

CREATE FUNCTION guard_human_preparation_attempt() RETURNS trigger AS $$
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION '准备尝试不可删除'; END IF;
  IF TG_OP='UPDATE' AND
    (ROW(NEW.id,NEW.job_id,NEW.number,NEW.lease_token,NEW.worker_id,NEW.started_at,
         NEW.lease_expires_at) IS DISTINCT FROM
     ROW(OLD.id,OLD.job_id,OLD.number,OLD.lease_token,OLD.worker_id,OLD.started_at,
         OLD.lease_expires_at)
     OR OLD.status<>'running') THEN
    RAISE EXCEPTION '准备尝试身份或已结算结果不可改写';
  END IF;
  IF TG_OP='INSERT' AND NEW.status<>'running' THEN
    RAISE EXCEPTION '准备尝试必须从运行状态开始';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER human_preparation_attempts_guard BEFORE INSERT OR UPDATE OR DELETE
  ON investigation_human_preparation_attempts FOR EACH ROW EXECUTE FUNCTION
  guard_human_preparation_attempt();

CREATE FUNCTION guard_human_preparation_artifact() RETURNS trigger AS $$
DECLARE
  job investigation_human_preparation_jobs%ROWTYPE;
  attempt investigation_human_preparation_attempts%ROWTYPE;
  run investigation_runs%ROWTYPE;
  current_version INTEGER;
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '准备工件不可更新或删除'; END IF;
  SELECT * INTO job FROM investigation_human_preparation_jobs WHERE id=NEW.job_id;
  IF job.id IS NULL THEN RAISE EXCEPTION '准备工件缺少任务'; END IF;
  PERFORM 1 FROM work_items WHERE investigation_run_id=job.investigation_run_id FOR UPDATE;
  SELECT * INTO job FROM investigation_human_preparation_jobs WHERE id=NEW.job_id FOR UPDATE;
  SELECT * INTO attempt FROM investigation_human_preparation_attempts WHERE id=NEW.attempt_id;
  SELECT * INTO run FROM investigation_runs WHERE id=job.investigation_run_id;
  SELECT max(state_version) INTO current_version FROM investigation_human_wait_events
    WHERE investigation_run_id=job.investigation_run_id;
  IF job.status<>'running' OR attempt.id IS NULL OR attempt.job_id<>job.id
    OR attempt.status<>'running' OR attempt.number<>job.attempt_count
    OR attempt.lease_token IS DISTINCT FROM job.lease_token
    OR attempt.worker_id IS DISTINCT FROM job.locked_by
    OR attempt.lease_expires_at IS DISTINCT FROM job.locked_until
    OR NEW.created_at<attempt.started_at OR NEW.created_at>=attempt.lease_expires_at
    OR NEW.created_at>=job.locked_until OR run.status IS DISTINCT FROM 'await_human'
    OR current_version IS DISTINCT FROM job.wait_version
    OR NEW.content->>'source_sha256' IS DISTINCT FROM job.source_sha256 THEN
    RAISE EXCEPTION '准备工件需要当前有效尝试租约及未变化的人工输入';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER human_preparation_artifacts_guard BEFORE INSERT OR UPDATE OR DELETE
  ON investigation_human_preparation_artifacts FOR EACH ROW EXECUTE FUNCTION
  guard_human_preparation_artifact();

CREATE FUNCTION guard_human_preparation_command() RETURNS trigger AS $$
DECLARE
  admission operator_authorizations%ROWTYPE;
  job investigation_human_preparation_jobs%ROWTYPE;
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '准备命令审计不可更新或删除'; END IF;
  SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
  IF admission.id IS NULL OR admission.policy_version<>'operator-auth-v1'
    OR admission.role<>'engineer' OR NOT admission.permissions ? 'manage_incident'
    OR admission.subject_id IS DISTINCT FROM NEW.subject_id
    OR admission.method<>'POST' OR admission.operation<>'prepare_investigation_human_input'
    OR admission.route<>'/api/v1/investigations/{run_id}/human-wait/preparations'
    OR admission.resources IS DISTINCT FROM
       jsonb_build_object('run_id',NEW.investigation_run_id::text)
    OR admission.authorized_at>NEW.created_at OR admission.body_sha256 IS DISTINCT FROM
       encode(sha256(convert_to(NEW.raw_body,'UTF8')),'hex') THEN
    RAISE EXCEPTION '准备命令缺少精确正文和同源可信授权';
  END IF;
  IF NEW.command->>'action' IS DISTINCT FROM 'prepare'
    OR NEW.command->>'handoff_id' IS NULL
    OR COALESCE(NEW.command->>'source_sha256','') !~ '^[0-9a-f]{64}$'
    OR jsonb_typeof(NEW.command->'expected_wait_version') IS DISTINCT FROM 'number'
    OR NEW.command->'expected_wait_version' NOT IN
       (SELECT to_jsonb(value) FROM generate_series(1,17) value)
    OR COALESCE(char_length(NEW.command->>'idempotency_key'),0) NOT BETWEEN 8 AND 100
    OR (NEW.command->>'idempotency_key') !~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'
    OR COALESCE(char_length(btrim(NEW.command->>'reason')),0) NOT BETWEEN 1 AND 1000 THEN
    RAISE EXCEPTION '准备命令缺少精确有界来源绑定';
  END IF;
  IF NEW.outcome IN ('applied','replayed') THEN
    SELECT * INTO job FROM investigation_human_preparation_jobs WHERE id=NEW.job_id;
    IF job.id IS NULL OR job.investigation_run_id IS DISTINCT FROM NEW.investigation_run_id
      OR job.requested_by_id IS DISTINCT FROM NEW.subject_id
      OR job.command_sha256 IS DISTINCT FROM NEW.command_sha256
      OR job.idempotency_key IS DISTINCT FROM NEW.command->>'idempotency_key'
      OR job.handoff_id::text IS DISTINCT FROM NEW.command->>'handoff_id'
      OR to_jsonb(job.wait_version) IS DISTINCT FROM NEW.command->'expected_wait_version'
      OR job.source #>> '{handoff,source_sha256}'
         IS DISTINCT FROM NEW.command->>'source_sha256'
      OR job.created_at>NEW.created_at
      OR (NEW.outcome='applied' AND (job.authorization_id IS DISTINCT FROM NEW.authorization_id
                                   OR job.created_at IS DISTINCT FROM NEW.created_at))
      OR (NEW.outcome='replayed' AND job.authorization_id=NEW.authorization_id) THEN
      RAISE EXCEPTION '准备命令结果与原始任务不一致';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER human_preparation_commands_guard BEFORE INSERT OR UPDATE OR DELETE
  ON investigation_human_preparation_commands FOR EACH ROW EXECUTE FUNCTION
  guard_human_preparation_command();

CREATE FUNCTION validate_human_preparation_state() RETURNS trigger AS $$
DECLARE
  job_id UUID;
  job investigation_human_preparation_jobs%ROWTYPE;
  attempt investigation_human_preparation_attempts%ROWTYPE;
  artifact investigation_human_preparation_artifacts%ROWTYPE;
  audit investigation_human_preparation_commands%ROWTYPE;
  attempts INTEGER;
BEGIN
  IF TG_TABLE_NAME='investigation_human_preparation_jobs' THEN job_id:=NEW.id;
  ELSE job_id:=NEW.job_id; END IF;
  SELECT * INTO job FROM investigation_human_preparation_jobs WHERE id=job_id;
  SELECT * INTO audit FROM investigation_human_preparation_commands
    WHERE authorization_id=job.authorization_id;
  SELECT count(*) INTO attempts FROM investigation_human_preparation_attempts
    WHERE investigation_human_preparation_attempts.job_id=job.id;
  SELECT * INTO attempt FROM investigation_human_preparation_attempts
    WHERE investigation_human_preparation_attempts.job_id=job.id
    ORDER BY number DESC LIMIT 1;
  SELECT * INTO artifact FROM investigation_human_preparation_artifacts
    WHERE investigation_human_preparation_artifacts.job_id=job.id;
  IF job.id IS NULL OR audit.authorization_id IS NULL OR audit.outcome<>'applied'
    OR audit.job_id IS DISTINCT FROM job.id OR audit.created_at IS DISTINCT FROM job.created_at
    OR audit.subject_id IS DISTINCT FROM job.requested_by_id
    OR audit.command_sha256 IS DISTINCT FROM job.command_sha256
    OR audit.investigation_run_id IS DISTINCT FROM job.investigation_run_id
    OR attempts<>job.attempt_count
    OR (attempt.id IS NOT NULL AND
        (attempt.number<>job.attempt_count OR attempt.started_at<job.created_at))
    OR EXISTS (SELECT 1 FROM investigation_human_preparation_attempts a
               WHERE a.job_id=job.id AND a.number<job.attempt_count
                 AND a.status IN ('running','succeeded')) THEN
    RAISE EXCEPTION '准备任务缺少同源成功命令或连续尝试';
  END IF;
  IF job.status='running' THEN
    IF attempt.id IS NULL OR attempt.status<>'running'
      OR attempt.lease_token IS DISTINCT FROM job.lease_token
      OR attempt.worker_id IS DISTINCT FROM job.locked_by
      OR attempt.lease_expires_at IS DISTINCT FROM job.locked_until
      OR artifact.job_id IS NOT NULL THEN
      RAISE EXCEPTION '准备任务与当前运行租约尝试不一致';
    END IF;
  ELSIF job.status='succeeded' THEN
    IF attempt.id IS NULL OR attempt.status<>'succeeded' OR artifact.job_id IS NULL
      OR artifact.attempt_id IS DISTINCT FROM attempt.id
      OR artifact.created_at IS DISTINCT FROM job.completed_at
      OR attempt.finished_at IS DISTINCT FROM artifact.created_at THEN
      RAISE EXCEPTION '准备成功需要同尝试和同时间的不可变工件';
    END IF;
  ELSIF artifact.job_id IS NOT NULL OR attempt.status IN ('running','succeeded') THEN
    RAISE EXCEPTION '未成功准备任务不能保留活动尝试或成功工件';
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER human_preparation_jobs_state AFTER INSERT OR UPDATE
  ON investigation_human_preparation_jobs DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION validate_human_preparation_state();
CREATE CONSTRAINT TRIGGER human_preparation_attempts_state AFTER INSERT OR UPDATE
  ON investigation_human_preparation_attempts DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION validate_human_preparation_state();
CREATE CONSTRAINT TRIGGER human_preparation_artifacts_state AFTER INSERT
  ON investigation_human_preparation_artifacts DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION validate_human_preparation_state();
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_human_preparation_jobs)
          OR EXISTS (SELECT 1 FROM investigation_human_preparation_attempts)
          OR EXISTS (SELECT 1 FROM investigation_human_preparation_artifacts)
          OR EXISTS (SELECT 1 FROM investigation_human_preparation_commands) THEN
          RAISE EXCEPTION '不能删除已保存的人工输入准备事实或命令审计';
        END IF;
      END $$;
      DROP TRIGGER human_preparation_jobs_state ON investigation_human_preparation_jobs;
      DROP TRIGGER human_preparation_attempts_state ON investigation_human_preparation_attempts;
      DROP TRIGGER human_preparation_artifacts_state ON investigation_human_preparation_artifacts;
      DROP FUNCTION validate_human_preparation_state();
      DROP TABLE investigation_human_preparation_commands;
      DROP TABLE investigation_human_preparation_artifacts;
      DROP TABLE investigation_human_preparation_attempts;
      DROP TABLE investigation_human_preparation_jobs;
      DROP FUNCTION guard_human_preparation_command();
      DROP FUNCTION guard_human_preparation_artifact();
      DROP FUNCTION guard_human_preparation_attempt();
      DROP FUNCTION guard_human_preparation_job();
    """)
