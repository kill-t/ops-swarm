"""持久候选评估、可信命令及原共享账本的独立执行归属。"""

from alembic import op

revision = "0072_candidate_evaluation"
down_revision = "0071_human_reassessment_routing"
branch_labels = None
depends_on = None

_OLD_CONSTRAINTS = {
    "ck_call_reservations_loop_payload": (
        "((operation IN ('read_log_evidence','read_metric_evidence','read_change_eviden"
        "ce','read_log_runbook','read_metric_runbook','read_change_runbook')) = (tool_r"
        "equest IS NOT NULL)) AND (tool_request IS NULL OR (jsonb_typeof(tool_request) "
        "= 'object' AND octet_length(tool_request::text) <= 8192)) AND (call_result IS "
        "NULL OR (status <> 'reserved' AND jsonb_typeof(call_result) = 'object' AND oct"
        "et_length(call_result::text) <= 65536 AND COALESCE(call_result ->> 'kind', '')"
        " = kind AND ((kind = 'model' AND ((operation = 'coordinate' AND COALESCE(model"
        "_input ->> 'version', '') IN ('coordination-model-input-v1','coordination-expe"
        "rience-input-v1') AND COALESCE(call_result ->> 'version', '') = 'coordination-"
        "model-result-v1') OR (operation = 'synthesize' AND NOT (model_input ? 'version"
        "') AND COALESCE(call_result ->> 'version', '') = 'open-hypothesis-synthesis-re"
        "sult-v1') OR (operation IN ('review', 'human_review') AND NOT (model_input ? '"
        "version') AND COALESCE(call_result ->> 'version', '') = 'hypothesis-review-res"
        "ult-v1') OR (operation NOT IN ('coordinate', 'synthesize', 'review', 'human_re"
        "view') AND COALESCE(model_input ->> 'version', '') IN ('evidence-loop-input-v1"
        "','runbook-loop-input-v1')))) OR (kind = 'tool' AND tool_request IS NOT NULL))"
        "))"
    ),
    "ck_call_reservations_invocation_index": (
        "invocation_index BETWEEN 0 AND 99 AND (operation <> 'coordinate' OR invocation"
        "_index = 0) AND (operation <> 'review' OR invocation_index IN (0, 1, 2)) AND ("
        "operation <> 'human_review' OR invocation_index = 0)"
    ),
    "ck_call_reservations_invocation_key": (
        "CASE WHEN operation='human_review' THEN idempotency_key='human-review:' || hum"
        "an_review_attempt_id::text ELSE idempotency_key = investigation_attempt_id::te"
        "xt || ':' || COALESCE(task_attempt_id::text, 'root') || ':' || operation || CA"
        "SE WHEN invocation_index = 0 THEN '' ELSE ':' || invocation_index::text END EN"
        "D"
    ),
    "ck_call_reservations_operation": (
        "(operation IN ('query_logs', 'query_metrics', 'list_changes', 'read_log_eviden"
        "ce', 'read_metric_evidence', 'read_change_evidence', 'read_log_runbook', 'read"
        "_metric_runbook', 'read_change_runbook') AND kind = 'tool') OR (operation IN ("
        "'coordinate', 'analyze_log', 'analyze_metric', 'analyze_change', 'synthesize',"
        " 'review', 'human_review') AND kind = 'model')"
    ),
    "ck_call_reservations_scope": (
        "(operation IN ('coordinate', 'synthesize', 'review', 'human_review') AND task_"
        "id IS NULL AND task_attempt_id IS NULL) OR (operation NOT IN ('coordinate', 's"
        "ynthesize', 'review', 'human_review') AND task_id IS NOT NULL AND task_attempt"
        "_id IS NOT NULL)"
    ),
}

_NEW_CONSTRAINTS = {
    "ck_call_reservations_loop_payload": (
        "((operation IN ('read_log_evidence','read_metric_evidence','read_change_eviden"
        "ce','read_log_runbook','read_metric_runbook','read_change_runbook')) = (tool_r"
        "equest IS NOT NULL)) AND (tool_request IS NULL OR (jsonb_typeof(tool_request) "
        "= 'object' AND octet_length(tool_request::text) <= 8192)) AND (call_result IS "
        "NULL OR (status <> 'reserved' AND jsonb_typeof(call_result) = 'object' AND oct"
        "et_length(call_result::text) <= 65536 AND COALESCE(call_result ->> 'kind', '')"
        " = kind AND ((kind = 'model' AND ((operation = 'coordinate' AND COALESCE(model"
        "_input ->> 'version', '') IN ('coordination-model-input-v1','coordination-expe"
        "rience-input-v1') AND COALESCE(call_result ->> 'version', '') = 'coordination-"
        "model-result-v1') OR (operation = 'synthesize' AND NOT (model_input ? 'version"
        "') AND COALESCE(call_result ->> 'version', '') = 'open-hypothesis-synthesis-re"
        "sult-v1') OR (operation IN ('review', 'human_review') AND NOT (model_input ? '"
        "version') AND COALESCE(call_result ->> 'version', '') = 'hypothesis-review-res"
        "ult-v1') OR (operation = 'evaluate_candidate' AND NOT (model_input ? 'version'"
        ") AND COALESCE(call_result ->> 'version', '') = 'candidate-model-result-v1') O"
        "R (operation NOT IN ('coordinate', 'synthesize', 'review', 'human_review', 'ev"
        "aluate_candidate') AND COALESCE(model_input ->> 'version', '') IN ('evidence-l"
        "oop-input-v1','runbook-loop-input-v1')))) OR (kind = 'tool' AND tool_request I"
        "S NOT NULL))))"
    ),
    "ck_call_reservations_invocation_index": (
        "invocation_index BETWEEN 0 AND 99 AND (operation <> 'coordinate' OR invocation"
        "_index = 0) AND (operation <> 'review' OR invocation_index IN (0, 1, 2)) AND ("
        "operation NOT IN ('human_review', 'evaluate_candidate') OR invocation_index = "
        "0)"
    ),
    "ck_call_reservations_invocation_key": (
        "CASE WHEN operation='human_review' THEN idempotency_key='human-review:' || hum"
        "an_review_attempt_id::text WHEN operation='evaluate_candidate' THEN idempotenc"
        "y_key='candidate:' || candidate_attempt_id::text ELSE idempotency_key = invest"
        "igation_attempt_id::text || ':' || COALESCE(task_attempt_id::text, 'root') || "
        "':' || operation || CASE WHEN invocation_index = 0 THEN '' ELSE ':' || invocat"
        "ion_index::text END END"
    ),
    "ck_call_reservations_operation": (
        "(operation IN ('query_logs', 'query_metrics', 'list_changes', 'read_log_eviden"
        "ce', 'read_metric_evidence', 'read_change_evidence', 'read_log_runbook', 'read"
        "_metric_runbook', 'read_change_runbook') AND kind = 'tool') OR (operation IN ("
        "'coordinate', 'analyze_log', 'analyze_metric', 'analyze_change', 'synthesize',"
        " 'review', 'human_review', 'evaluate_candidate') AND kind = 'model')"
    ),
    "ck_call_reservations_scope": (
        "(operation IN ('coordinate', 'synthesize', 'review', 'human_review', 'evaluate"
        "_candidate') AND task_id IS NULL AND task_attempt_id IS NULL) OR (operation NO"
        "T IN ('coordinate', 'synthesize', 'review', 'human_review', 'evaluate_candidat"
        "e') AND task_id IS NOT NULL AND task_attempt_id IS NOT NULL)"
    ),
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
    op.execute("ALTER TABLE investigation_call_reservations ADD COLUMN candidate_attempt_id UUID")
    _constraints(_NEW_CONSTRAINTS)
    op.execute("""
      ALTER TABLE investigation_call_reservations
        ADD CONSTRAINT ck_call_reservations_candidate_owner CHECK
          ((operation='evaluate_candidate') = (candidate_attempt_id IS NOT NULL)),
        ADD CONSTRAINT fk_call_reservations_candidate_attempt
          FOREIGN KEY (candidate_attempt_id,investigation_run_id)
          REFERENCES investigation_candidate_evaluation_attempts(id,investigation_run_id)
          ON DELETE RESTRICT;
    """)
    op.execute(_SOURCE)
    op.execute(_GUARDS)


_TABLES = """

CREATE TABLE investigation_candidate_evaluation_jobs (
id UUID NOT NULL,
investigation_run_id UUID NOT NULL,
source_id UUID NOT NULL,
source_kind VARCHAR(24) NOT NULL,
source JSONB NOT NULL,
source_content_sha256 VARCHAR(64) NOT NULL,
packet JSONB NOT NULL,
packet_sha256 VARCHAR(64) NOT NULL,
wait_version INTEGER,
policy_version VARCHAR(40) NOT NULL,
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
CONSTRAINT uq_candidate_jobs_source UNIQUE (investigation_run_id,source_kind,source_id),
CONSTRAINT ck_candidate_jobs_material CHECK (jsonb_typeof(source)='object' AND
  octet_length(source::text)<=2097152 AND jsonb_typeof(packet)='object' AND
  octet_length(packet::text)<=1048576),
CONSTRAINT ck_candidate_jobs_material_hash CHECK (source_content_sha256 ~ '^[0-9a-f]{64}$' AND
  packet_sha256 ~ '^[0-9a-f]{64}$'),
CONSTRAINT uq_candidate_evaluation_jobs_id_run UNIQUE (id, investigation_run_id),
CONSTRAINT uq_candidate_evaluation_jobs_key UNIQUE (investigation_run_id, requested_by_id,
idempotency_key),
CONSTRAINT uq_candidate_evaluation_jobs_authorization UNIQUE (authorization_id),
CONSTRAINT ck_candidate_jobs_source_version CHECK (((source_kind='terminal_handoff' AND
  wait_version IS NULL) OR (source_kind='human_route' AND wait_version BETWEEN 2 AND 16)) IS
  TRUE),
CONSTRAINT ck_candidate_evaluation_jobs_policy CHECK (policy_version='candidate-evaluation-v1'),
CONSTRAINT ck_candidate_evaluation_jobs_hashes CHECK (source_sha256 ~ '^[0-9a-f]{64}$' AND
command_sha256 ~ '^[0-9a-f]{64}$'),
CONSTRAINT ck_candidate_evaluation_jobs_subject CHECK (char_length(btrim(requested_by_id))
  BETWEEN 1
AND 100),
CONSTRAINT ck_candidate_evaluation_jobs_key CHECK (char_length(idempotency_key) BETWEEN 8 AND 100
  AND
idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'),
CONSTRAINT ck_candidate_evaluation_jobs_status CHECK (status IN
('queued','running','succeeded','failed')),
CONSTRAINT ck_candidate_evaluation_jobs_attempts CHECK (attempt_count BETWEEN 0 AND 3),
CONSTRAINT ck_candidate_evaluation_jobs_lease CHECK ((status='running' AND locked_by IS NOT NULL
  AND
lease_token IS NOT NULL AND locked_until IS NOT NULL) OR (status<>'running' AND locked_by IS
NULL AND lease_token IS NULL AND locked_until IS NULL)),
CONSTRAINT ck_candidate_evaluation_jobs_completion CHECK ((status IN ('queued','running') AND
completed_at IS NULL) OR (status IN ('succeeded','failed') AND completed_at IS NOT NULL)),
CONSTRAINT ck_candidate_evaluation_jobs_error CHECK (status<>'failed' OR last_error_code IS NOT
  NULL),
CONSTRAINT ck_candidate_evaluation_jobs_time CHECK (created_at<=updated_at AND
available_at>=created_at AND (completed_at IS NULL OR completed_at>=created_at)),
FOREIGN KEY(authorization_id) REFERENCES operator_authorizations (id) ON DELETE RESTRICT
);

CREATE INDEX ix_candidate_evaluation_jobs_claim ON investigation_candidate_evaluation_jobs
  (status,
available_at, created_at);

CREATE TABLE investigation_candidate_evaluation_attempts (
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
CONSTRAINT uq_candidate_evaluation_attempts_id_run UNIQUE (id, investigation_run_id),
CONSTRAINT uq_candidate_evaluation_attempts_scope UNIQUE (id, job_id, investigation_run_id),
CONSTRAINT fk_candidate_evaluation_attempts_job FOREIGN KEY(job_id, investigation_run_id)
  REFERENCES
investigation_candidate_evaluation_jobs (id, investigation_run_id) ON DELETE RESTRICT,
CONSTRAINT uq_candidate_evaluation_attempts_number UNIQUE (job_id, number),
CONSTRAINT uq_candidate_evaluation_attempts_id_job UNIQUE (id, job_id),
CONSTRAINT uq_candidate_evaluation_attempts_token UNIQUE (lease_token),
CONSTRAINT ck_candidate_evaluation_attempts_number CHECK (number BETWEEN 1 AND 3),
CONSTRAINT ck_candidate_evaluation_attempts_status CHECK (status IN
('running','succeeded','failed','lease_expired')),
CONSTRAINT ck_candidate_evaluation_attempts_completion CHECK ((status='running' AND finished_at IS
NULL AND error_code IS NULL) OR (status='succeeded' AND finished_at IS NOT NULL AND error_code
IS NULL) OR (status IN ('failed','lease_expired') AND finished_at IS NOT NULL AND error_code
IS NOT NULL)),
CONSTRAINT ck_candidate_evaluation_attempts_time CHECK (lease_expires_at>started_at AND
  (finished_at
IS NULL OR finished_at>=started_at))
);

CREATE TABLE investigation_candidate_evaluation_results (
job_id UUID NOT NULL,
attempt_id UUID NOT NULL,
source_call_id UUID NOT NULL,
content JSONB NOT NULL,
content_sha256 VARCHAR(64) NOT NULL,
created_at TIMESTAMP WITH TIME ZONE NOT NULL,
PRIMARY KEY (job_id),
CONSTRAINT fk_candidate_evaluation_results_attempt FOREIGN KEY(attempt_id, job_id) REFERENCES
investigation_candidate_evaluation_attempts (id, job_id) ON DELETE RESTRICT,
CONSTRAINT ck_candidate_evaluation_results_content CHECK (jsonb_typeof(content)='object' AND
octet_length(content::text)<=65536),
CONSTRAINT ck_candidate_evaluation_results_hash CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
FOREIGN KEY(job_id) REFERENCES investigation_candidate_evaluation_jobs (id) ON DELETE RESTRICT,
FOREIGN KEY(source_call_id) REFERENCES investigation_call_reservations (id) ON DELETE RESTRICT
);

CREATE TABLE investigation_candidate_evaluation_commands (
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
CONSTRAINT uq_candidate_evaluation_commands_authorization UNIQUE (authorization_id),
CONSTRAINT ck_candidate_evaluation_commands_body CHECK (jsonb_typeof(command)='object' AND
octet_length(command::text)<=32768),
CONSTRAINT ck_candidate_evaluation_commands_raw CHECK (octet_length(raw_body) BETWEEN 2 AND
  65536),
CONSTRAINT ck_candidate_evaluation_commands_hash CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
CONSTRAINT ck_candidate_evaluation_commands_subject CHECK (char_length(btrim(subject_id)) BETWEEN
  1
AND 100),
CONSTRAINT ck_candidate_evaluation_commands_outcome CHECK ((outcome IN ('applied','replayed') AND
job_id IS NOT NULL AND error_code IS NULL AND error_message IS NULL AND ((outcome='applied'
AND http_status=201) OR (outcome='replayed' AND http_status=200))) OR (outcome='error' AND
job_id IS NULL AND error_code IS NOT NULL AND error_message IS NOT NULL AND http_status IN
(404,409))),
CONSTRAINT ck_candidate_evaluation_commands_error CHECK (error_code IS NULL OR error_code ~
'^[a-z0-9_]{1,100}$'),
FOREIGN KEY(authorization_id) REFERENCES operator_authorizations (id) ON DELETE RESTRICT,
FOREIGN KEY(job_id) REFERENCES investigation_candidate_evaluation_jobs (id) ON DELETE RESTRICT
);

CREATE INDEX ix_candidate_evaluation_commands_run_cursor ON
  investigation_candidate_evaluation_commands
(investigation_run_id, cursor);
"""

_SOURCE = """

CREATE FUNCTION candidate_canonical_json(value JSONB) RETURNS TEXT AS $$
DECLARE result TEXT;
BEGIN
  CASE jsonb_typeof(value)
    WHEN 'object' THEN
      SELECT '{'||COALESCE(string_agg(to_jsonb(key)::text||':'||candidate_canonical_json(item),
          ',' ORDER BY key COLLATE "C"),'')||'}' INTO result FROM jsonb_each(value) e(key,item);
    WHEN 'array' THEN
      SELECT '['||COALESCE(string_agg(candidate_canonical_json(item),',' ORDER BY
  ordinal),'')||']'
        INTO result FROM jsonb_array_elements(value) WITH ORDINALITY e(item,ordinal);
    ELSE result:=value::text;
  END CASE;
  RETURN result;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

CREATE FUNCTION assert_candidate_source(
  job investigation_candidate_evaluation_jobs, require_current BOOLEAN
) RETURNS VOID AS $$
DECLARE
  run investigation_runs%ROWTYPE;
  handoff investigation_terminal_handoffs%ROWTYPE;
  route investigation_hypothesis_review_routes%ROWTYPE;
  review investigation_hypothesis_reviews%ROWTYPE;
  hypothesis investigation_open_hypotheses%ROWTYPE;
  original_call investigation_call_reservations%ROWTYPE;
  human_route investigation_human_review_routes%ROWTYPE;
  human_job investigation_human_review_jobs%ROWTYPE;
  human_result investigation_human_review_results%ROWTYPE;
  preparation investigation_human_preparation_jobs%ROWTYPE;
  material JSONB;
  supplied JSONB;
  expected_review JSONB;
  encoded_input BYTEA;
  current_version INTEGER;
BEGIN
  SELECT * INTO run FROM investigation_runs WHERE id=job.investigation_run_id;
  SELECT * INTO handoff FROM investigation_terminal_handoffs
    WHERE investigation_run_id=job.investigation_run_id;
  SELECT * INTO route FROM investigation_hypothesis_review_routes
    WHERE id=handoff.source_review_route_id;
  SELECT * INTO review FROM investigation_hypothesis_reviews
    WHERE id=route.hypothesis_review_id;
  SELECT * INTO hypothesis FROM investigation_open_hypotheses
    WHERE id=route.open_hypothesis_id;
  SELECT * INTO original_call FROM investigation_call_reservations
    WHERE id=review.source_call_id;
  IF (run.id IS NOT NULL AND handoff.id IS NOT NULL AND route.id IS NOT NULL
    AND review.id IS NOT NULL AND hypothesis.id IS NOT NULL
    AND original_call.status='succeeded' AND original_call.operation='review'
    AND run.workflow_version='v3' AND run.graph_version='dynamic-investigation-graph-v15'
    AND run.data_source_mode='frozen' AND run.mode='multi_agent'
    AND original_call.investigation_run_id=run.id
    AND route.investigation_run_id=run.id AND review.investigation_run_id=run.id
    AND hypothesis.investigation_run_id=run.id
    AND route.content_sha256=handoff.source_sha256
    AND route.open_hypothesis_id=review.open_hypothesis_id
    AND route.round_id=review.round_id AND route.round_id=hypothesis.round_id
    AND job.created_at>=handoff.created_at
    AND job.source->>'version'='candidate-source-v1'
    AND job.source->>'source_kind'=job.source_kind
    AND job.source->>'source_id'=job.source_id::text
    AND job.source->>'source_sha256'=job.source_sha256
    AND (job.source->'handoff')-'created_at'=to_jsonb(handoff)-'created_at'
    AND (job.source #>> '{handoff,created_at}')::timestamptz=handoff.created_at
    AND (job.source->'original_route')-'created_at'=to_jsonb(route)-'created_at'
    AND (job.source #>> '{original_route,created_at}')::timestamptz=route.created_at
    AND (job.source->'hypothesis')-'created_at'=to_jsonb(hypothesis)-'created_at'
    AND (job.source #>> '{hypothesis,created_at}')::timestamptz=hypothesis.created_at
    AND job.source->'prior_input'=original_call.model_input
    AND job.source->'call_budget'=run.call_budget
    AND job.source->'model_budget'=run.model_budget
    AND (job.source->>'started_at')::timestamptz=run.started_at
    AND EXISTS (SELECT 1 FROM incidents i WHERE i.id=run.incident_id
                AND i.service_name=job.source->>'service_name')
    AND NOT EXISTS (SELECT 1 FROM investigation_rounds r
        WHERE r.investigation_run_id=run.id AND (r.status<>'completed' OR r.round_number>
          (SELECT round_number FROM investigation_rounds WHERE id=route.round_id)))
    AND NOT EXISTS (SELECT 1 FROM agent_tasks t
                    WHERE t.investigation_run_id=run.id AND t.status<>'succeeded')) IS NOT TRUE
  THEN
    RAISE EXCEPTION '候选来源缺少同运行最高终端及完整原审查材料';
  END IF;
  SELECT (m->>'content')::jsonb INTO material
    FROM jsonb_array_elements(original_call.model_input->'messages') m
    WHERE m->>'role'='user';
  IF (job.source->'hypothesis'=material->'open_hypothesis'
      AND job.source->'evidence'=material->'evidence') IS NOT TRUE THEN
    RAISE EXCEPTION '候选来源不能替换原假设或证据闭包';
  END IF;
  expected_review:=review.review-'status'-'version' ||
    jsonb_build_object('version','hypothesis-review-proposal-v1');
  IF job.source_kind='terminal_handoff' THEN
    IF (handoff.destination='candidate_evaluation' AND job.source_id=handoff.id
      AND job.wait_version IS NULL AND route.decision->>'route'='candidate_evaluation'
      AND job.source->'human_route'='null'::jsonb
      AND job.source->'human_inputs'='[]'::jsonb) IS NOT TRUE THEN
      RAISE EXCEPTION '候选原交接来源不具备候选意图';
    END IF;
    IF job.source_sha256 IS DISTINCT FROM encode(sha256(convert_to(
      candidate_canonical_json(job.source->'handoff'),'UTF8')),'hex') THEN
      RAISE EXCEPTION '候选原交接引用摘要不一致';
    END IF;
  ELSIF job.source_kind='human_route' THEN
    SELECT * INTO human_route FROM investigation_human_review_routes WHERE id=job.source_id;
    SELECT * INTO human_job FROM investigation_human_review_jobs WHERE id=human_route.job_id;
    SELECT * INTO human_result FROM investigation_human_review_results WHERE job_id=human_job.id;
    SELECT * INTO preparation FROM investigation_human_preparation_jobs
      WHERE id=human_job.preparation_job_id;
    IF (human_route.id IS NOT NULL AND human_job.status='succeeded'
      AND human_result.job_id=human_job.id
      AND human_route.investigation_run_id=run.id AND human_job.investigation_run_id=run.id
      AND preparation.investigation_run_id=run.id AND preparation.handoff_id=handoff.id
      AND handoff.destination='await_human'
      AND human_route.wait_version=job.wait_version AND human_job.wait_version=job.wait_version
      AND human_route.result_sha256=human_result.content_sha256
      AND human_route.source_call_id=human_result.source_call_id
      AND human_route.preparation_source_sha256=preparation.source_sha256
      AND human_route.decision->>'route'='candidate_evaluation'
      AND human_route.decision->>'dispatch_status'='not_dispatched'
      AND human_route.decision->'blocking_reason_codes'='[]'::jsonb
      AND human_route.decision->'unresolved_constraints'='[]'::jsonb
      AND job.created_at>=human_route.created_at
      AND (job.source->'human_route')-'created_at'=to_jsonb(human_route)-'created_at'
      AND (job.source #>> '{human_route,created_at}')::timestamptz=human_route.created_at
      AND job.source->'human_inputs'=preparation.source #> '{wait,events}'
      AND jsonb_typeof(job.source->'human_inputs')='array'
      AND jsonb_array_length(job.source->'human_inputs')=job.wait_version-1) IS NOT TRUE THEN
      RAISE EXCEPTION '候选重评来源缺少当前成功意见及完整人工前缀';
    END IF;
    FOR supplied IN SELECT value FROM jsonb_array_elements(job.source->'human_inputs') LOOP
      IF NOT EXISTS (SELECT 1 FROM investigation_human_wait_events e
        JOIN investigation_human_wait_commands audit
          ON audit.authorization_id=e.authorization_id AND audit.event_id=e.id
            AND audit.outcome='applied' AND audit.command_sha256=e.command_sha256
        WHERE e.id::text=supplied->>'id' AND e.investigation_run_id=run.id
          AND e.handoff_id=handoff.id AND e.action='supplement'
          AND e.state_version<=job.wait_version
          AND supplied-'created_at'=(to_jsonb(e)-'created_at'-'idempotency_key')||
              jsonb_build_object('command',audit.command)
          AND (supplied->>'created_at')::timestamptz=e.created_at)
        OR EXISTS (SELECT 1 FROM jsonb_array_elements(supplied #> '{command,items}') item
                   WHERE item->>'kind'='constraint') THEN
        RAISE EXCEPTION '候选不能丢失人工前缀或解除人工约束';
      END IF;
    END LOOP;
    expected_review:=human_result.content->'proposal';
    IF job.source_sha256 IS DISTINCT FROM encode(sha256(convert_to(
      candidate_canonical_json(job.source->'human_route'),'UTF8')),'hex') THEN
      RAISE EXCEPTION '候选重评引用摘要不一致';
    END IF;
  ELSE RAISE EXCEPTION '候选来源类型不受支持';
  END IF;
  encoded_input:=convert_to(candidate_canonical_json(job.packet->'model_input'),'UTF8');
  supplied:=(job.packet #>> '{model_input,messages,1,content}')::jsonb;
  IF (job.source->'review'=expected_review
    AND job.source #>> '{review,support_is_sufficient}'='true'
    AND job.source #> '{review,counter_evidence_ids}'='[]'::jsonb
    AND job.source #> '{review,evidence_gaps}'='[]'::jsonb
    AND job.source #> '{review,competing_explanations}'='[]'::jsonb
    AND job.source #> '{hypothesis,hypothesis,counter_evidence_ids}'='[]'::jsonb
    AND job.packet #>> '{manifest,version}'='candidate-context-v1'
    AND job.packet #>> '{manifest,policy_version}'='candidate-evaluation-v1'
    AND job.packet #>> '{manifest,prompt_version}'='candidate-assessment-v1'
    AND job.packet #>> '{manifest,capability_catalog_version}'='action-candidate-catalog-v1'
    AND job.packet #>> '{manifest,source_sha256}'=job.source_content_sha256
    AND job.packet #> '{manifest,tool_names}'='[]'::jsonb
    AND job.packet #> '{manifest,trimming}'='[]'::jsonb
    AND job.packet #>> '{manifest,input_sha256}'=job.packet #>> '{allocation,input_sha256}'
    AND job.packet #>> '{allocation,prompt_version}'='candidate-assessment-v1'
    AND job.packet #>> '{allocation,estimator_version}'='utf8-json-v1'
    AND job.packet #>> '{allocation,input_sha256}'=encode(sha256(encoded_input),'hex')
    AND (job.packet #>> '{allocation,input_bytes}')::numeric=octet_length(encoded_input)
    AND (job.packet #>> '{allocation,estimated_input_tokens}')::numeric=
        octet_length(encoded_input)+256
    AND (job.packet #>> '{allocation,estimated_input_tokens}')::numeric BETWEEN 1 AND
        (run.model_budget->>'max_input_tokens_per_call')::numeric
    AND (job.packet #>> '{allocation,output_token_limit}')::numeric=
        (run.model_budget->>'max_output_tokens_per_call')::numeric
    AND (job.packet #>> '{allocation,estimated_input_tokens}')::numeric+
        (job.packet #>> '{allocation,output_token_limit}')::numeric+
        (run.model_budget->>'safety_margin_tokens')::numeric<=
        (run.model_budget #>> '{profile,context_window_tokens}')::numeric
    AND (job.packet #>> '{allocation,reserved_cost_usd}')::numeric=ceil(
        (job.packet #>> '{allocation,estimated_input_tokens}')::numeric*
        (run.model_budget #>> '{profile,pricing,input_cost_per_million_usd}')::numeric+
        (job.packet #>> '{allocation,output_token_limit}')::numeric*
        (run.model_budget #>> '{profile,pricing,output_cost_per_million_usd}')::numeric)/1000000
    AND job.packet_sha256=encode(sha256(convert_to(
        candidate_canonical_json(job.packet),'UTF8')),'hex')
    AND jsonb_array_length(job.packet #> '{model_input,messages}')=2
    AND job.packet #>> '{model_input,messages,0,role}'='system'
    AND job.packet #>> '{model_input,messages,1,role}'='user'
    AND supplied->'original_investigation'=material
    AND supplied->'current_review'=job.source->'review'
    AND supplied->'human_inputs'=job.source->'human_inputs'
    AND supplied->>'capability_catalog_version'='action-candidate-catalog-v1'
    AND job.packet #>> '{model_input,structured_output_method}'='function_calling'
    AND job.packet #>> '{model_input,tool_schema,type}'='function'
    AND job.packet #>> '{model_input,tool_schema,function,name}'='CandidateProposal'
    AND NOT (job.packet->'model_input' ? 'version')) IS NOT TRUE THEN
    RAISE EXCEPTION '候选来源、无工具上下文或当前审查不一致';
  END IF;
  IF require_current THEN
    SELECT max(state_version) INTO current_version FROM investigation_human_wait_events
      WHERE investigation_run_id=run.id;
    PERFORM 1 FROM incidents WHERE id=run.incident_id AND status='investigating' FOR UPDATE;
    IF NOT FOUND OR (job.source_kind='terminal_handoff' AND run.status<>'await_candidate')
      OR (job.source_kind='human_route' AND
          (run.status<>'await_human' OR current_version IS DISTINCT FROM job.wait_version)) THEN
      RAISE EXCEPTION '候选来源已经取消、补充或不再允许调查';
    END IF;
  END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION assert_candidate_proposal(proposal JSONB, source JSONB) RETURNS VOID AS $$
BEGIN
  IF (jsonb_typeof(proposal)='object'
    AND (proposal-ARRAY['status','action_type','rationale','evidence_ids','preconditions',
        'unresolved_questions'])='{}'::jsonb
    AND proposal->>'status' IN ('candidate','unsupported')
    AND jsonb_typeof(proposal->'rationale')='string'
    AND char_length(btrim(proposal->>'rationale')) BETWEEN 1 AND 1000
    AND jsonb_typeof(proposal->'evidence_ids')='array'
    AND jsonb_array_length(proposal->'evidence_ids')<=100
    AND jsonb_typeof(proposal->'preconditions')='array'
    AND jsonb_array_length(proposal->'preconditions')<=10
    AND jsonb_typeof(proposal->'unresolved_questions')='array'
    AND jsonb_array_length(proposal->'unresolved_questions')<=10
    AND ((proposal->>'status'='unsupported' AND proposal->'action_type'='null'::jsonb)
      OR (proposal->>'status'='candidate' AND proposal->>'action_type' IN
          ('rollback_release','rollback_configuration','restore_dependency')
          AND jsonb_array_length(proposal->'evidence_ids')>0
          AND jsonb_array_length(proposal->'preconditions')>0))) IS NOT TRUE THEN
    RAISE EXCEPTION '候选提案不符合无参数白名单输出契约';
  END IF;
  IF EXISTS (SELECT 1 FROM jsonb_array_elements(proposal->'evidence_ids') e
      WHERE jsonb_typeof(e)<>'string' OR trim(both '"' from e::text) !~
  '^[a-z0-9][a-z0-9-]{0,99}$'
        OR NOT EXISTS (SELECT 1 FROM jsonb_array_elements(source->'evidence') original
                       WHERE to_jsonb(original->>'evidence_id')=e))
    OR jsonb_array_length(proposal->'evidence_ids')<>
       (SELECT count(DISTINCT e) FROM jsonb_array_elements(proposal->'evidence_ids') e)
    OR jsonb_array_length(proposal->'preconditions')<>
       (SELECT count(DISTINCT e) FROM jsonb_array_elements(proposal->'preconditions') e)
    OR jsonb_array_length(proposal->'unresolved_questions')<>
       (SELECT count(DISTINCT e) FROM jsonb_array_elements(proposal->'unresolved_questions') e)
    OR EXISTS (SELECT 1 FROM jsonb_array_elements(proposal->'preconditions') e
               WHERE jsonb_typeof(e)<>'string' OR char_length(btrim(e#>>'{}')) NOT BETWEEN 1 AND
  500)
    OR EXISTS (SELECT 1 FROM jsonb_array_elements(proposal->'unresolved_questions') e
               WHERE jsonb_typeof(e)<>'string' OR char_length(btrim(e#>>'{}')) NOT BETWEEN 1 AND
  500)
    OR (proposal->>'status'='candidate' AND NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements(proposal->'evidence_ids') e
        WHERE source #> '{review,accepted_supporting_evidence_ids}' @> jsonb_build_array(e))) THEN
    RAISE EXCEPTION '候选提案引用越过证据闭包或未引用当前接受的支持';
  END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION assert_candidate_step(content JSONB, source JSONB) RETURNS VOID AS $$
DECLARE
  step JSONB;
  parameters JSONB;
  observation JSONB;
  action TEXT;
  signal TEXT;
  threshold NUMERIC;
  rule TEXT;
  rationale TEXT;
BEGIN
  PERFORM assert_candidate_proposal(content->'proposal',source);
  IF (content-ARRAY['version','proposal','disposition','step','reason_code','execution_eligible'])<>
      '{}'::jsonb THEN RAISE EXCEPTION '候选评估不能携带根因映射或额外授权字段'; END IF;
  step:=content->'step'; action:=content #>> '{proposal,action_type}';
  IF content->>'disposition'='await_human' THEN
    IF step IS DISTINCT FROM 'null'::jsonb THEN
      RAISE EXCEPTION '等待人工的候选不能保存动作步骤';
    END IF;
    RETURN;
  END IF;
  IF (content->>'disposition'='planning_review' AND content #>> '{proposal,status}'='candidate'
      AND step->>'action_type'=action AND step->'step_order'='1'::jsonb
      AND step->>'risk_level'='high' AND step->'requires_human_approval'='true'::jsonb
      AND (step-ARRAY['step_order','action_type','target_type','target_name','parameters',
           'risk_level','requires_human_approval','risk_rule_code','risk_rationale','verification'])=
          '{}'::jsonb)
       IS NOT TRUE THEN RAISE EXCEPTION '候选步骤不能修改动作、风险或审批要求'; END IF;
  IF action='rollback_release' THEN
    SELECT e INTO observation FROM jsonb_array_elements(source->'evidence')
      WITH ORDINALITY observations(e,position)
      WHERE content #> '{proposal,evidence_ids}' @> jsonb_build_array(e->>'evidence_id')
        AND e->>'kind'='change' AND e #>> '{attributes,change_type}'='deployment'
        AND e #> '{attributes,completed}'='true'::jsonb
        AND jsonb_typeof(e #> '{attributes,from_version}')='string'
        AND jsonb_typeof(e #> '{attributes,to_version}')='string'
        AND e #>> '{attributes,from_version}' ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'
        AND e #>> '{attributes,to_version}' ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'
        AND e #> '{attributes,from_version}'<>e #> '{attributes,to_version}'
      ORDER BY (e->>'observed_at')::timestamptz DESC,position LIMIT 1;
    parameters:=jsonb_build_object('current_version',observation #>> '{attributes,to_version}',
                                  'target_version',observation #>> '{attributes,from_version}');
    signal:='http_5xx_ratio'; threshold:=0.01; rule:='rollback_changes_running_version';
    rationale:='回滚会把运行版本从 '||(parameters->>'current_version')||' 改为 '||
      (parameters->>'target_version')||'，必须由工程师审批。';
  ELSIF action='rollback_configuration' THEN
    SELECT e INTO observation FROM jsonb_array_elements(source->'evidence')
      WITH ORDINALITY observations(e,position)
      WHERE content #> '{proposal,evidence_ids}' @> jsonb_build_array(e->>'evidence_id')
        AND e->>'kind'='change' AND e #>> '{attributes,change_type}'='configuration'
        AND e #>> '{attributes,key}'='database_pool_size'
        AND e #> '{attributes,completed}'='true'::jsonb
        AND CASE WHEN jsonb_typeof(e #> '{attributes,from_value}')='number'
                      AND e #>> '{attributes,from_value}' ~ '^[0-9]+$'
                 THEN (e #>> '{attributes,from_value}')::numeric BETWEEN 1 AND 1000
                 ELSE FALSE END
        AND CASE WHEN jsonb_typeof(e #> '{attributes,to_value}')='number'
                      AND e #>> '{attributes,to_value}' ~ '^[0-9]+$'
                 THEN (e #>> '{attributes,to_value}')::numeric BETWEEN 1 AND 1000
                 ELSE FALSE END
        AND e #> '{attributes,from_value}'<>e #> '{attributes,to_value}'
      ORDER BY (e->>'observed_at')::timestamptz DESC,position LIMIT 1;
    parameters:=jsonb_build_object('key','database_pool_size',
        'current_value',observation #> '{attributes,to_value}',
        'target_value',observation #> '{attributes,from_value}');
    signal:='database_pool_saturation'; threshold:=0.8;
    rule:='rollback_changes_runtime_configuration';
    rationale:='回滚会把 database_pool_size 从 '||(parameters->>'current_value')||' 改为 '||
      (parameters->>'target_value')||'，必须由工程师审批。';
  ELSE
    SELECT e INTO observation FROM jsonb_array_elements(source->'evidence') e
      WHERE content #> '{proposal,evidence_ids}' @> jsonb_build_array(e->>'evidence_id')
        AND e->>'kind'='metric' AND e #> '{attributes,redis_healthy}'='false'::jsonb LIMIT 1;
    parameters:=jsonb_build_object('dependency','redis',
                                  'expected_available',FALSE,'target_available',TRUE);
    signal:='redis_unavailable_ratio'; threshold:=0.01;
    rule:='restore_changes_dependency_state';
    rationale:='恢复 Redis 会改变运行依赖状态，必须由工程师审批。';
  END IF;
  IF (observation IS NOT NULL AND step->'parameters'=parameters
    AND step->>'target_type'=CASE WHEN action='restore_dependency' THEN 'dependency' ELSE
  'service' END
    AND step->>'target_name'=CASE WHEN action='restore_dependency' THEN 'redis' ELSE
  source->>'service_name' END
    AND step->>'risk_rule_code'=rule AND step->>'risk_rationale'=rationale
    AND step->'verification'=jsonb_build_object('signal',signal,'operator','less_than',
       'threshold',threshold,'observation_window_seconds',60,'required_successful_samples',3))
      IS NOT TRUE THEN
    RAISE EXCEPTION '候选步骤缺少所引用证据的安全参数或确定性验证规则';
  END IF;
END;
$$ LANGUAGE plpgsql;
"""

_JOB_GUARD = """

CREATE FUNCTION guard_candidate_evaluation_job() RETURNS trigger AS $$
DECLARE admission operator_authorizations%ROWTYPE;
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION '候选评估任务不可删除'; END IF;
  PERFORM 1 FROM work_items WHERE investigation_run_id=NEW.investigation_run_id FOR UPDATE;
  IF TG_OP='UPDATE' THEN
    IF ROW(NEW.id,NEW.investigation_run_id,NEW.source_kind,NEW.source_id,NEW.source_sha256,
        NEW.source,NEW.source_content_sha256,NEW.packet,NEW.packet_sha256,
        NEW.wait_version,NEW.policy_version,NEW.requested_by_id,NEW.authorization_id,
        NEW.idempotency_key,NEW.command_sha256,NEW.created_at) IS DISTINCT FROM
       ROW(OLD.id,OLD.investigation_run_id,OLD.source_kind,OLD.source_id,OLD.source_sha256,
        OLD.source,OLD.source_content_sha256,OLD.packet,OLD.packet_sha256,
        OLD.wait_version,OLD.policy_version,OLD.requested_by_id,OLD.authorization_id,
        OLD.idempotency_key,OLD.command_sha256,OLD.created_at)
       OR (OLD.status IN ('succeeded','failed') AND NEW IS DISTINCT FROM OLD) THEN
      RAISE EXCEPTION '候选评估任务固定来源或终态不可改写';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.status<>'queued' OR NEW.attempt_count<>0 THEN
    RAISE EXCEPTION '候选评估任务必须从未执行队列状态开始';
  END IF;
  PERFORM assert_candidate_source(NEW,TRUE);
  IF NEW.created_at>=(NEW.source->>'started_at')::timestamptz+
     make_interval(secs=>(NEW.source #>> '{call_budget,max_duration_seconds}')::int) THEN
    RAISE EXCEPTION '候选评估任务不能延长原运行时限';
  END IF;
  SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
  IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
    AND admission.role='engineer' AND admission.permissions ? 'manage_incident'
    AND admission.subject_id=NEW.requested_by_id AND admission.method='POST'
    AND admission.operation='evaluate_investigation_candidate'
    AND admission.route='/api/v1/investigations/{run_id}/candidate-evaluations'
    AND admission.resources=jsonb_build_object('run_id',NEW.investigation_run_id::text)
    AND admission.authorized_at<=NEW.created_at) IS NOT TRUE THEN
    RAISE EXCEPTION '候选评估任务缺少同源可信工程师准入';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_evaluation_jobs_guard BEFORE INSERT OR UPDATE OR DELETE
  ON investigation_candidate_evaluation_jobs FOR EACH ROW
  EXECUTE FUNCTION guard_candidate_evaluation_job();
"""

_COMMAND_GUARD = """

CREATE FUNCTION candidate_strip_text(value TEXT) RETURNS TEXT AS $$
  SELECT btrim(value,chr(9)||chr(10)||chr(11)||chr(12)||chr(13)||chr(32)||chr(133)||
    chr(160)||chr(5760)||chr(8192)||chr(8193)||chr(8194)||chr(8195)||chr(8196)||
    chr(8197)||chr(8198)||chr(8199)||chr(8200)||chr(8201)||chr(8202)||chr(8232)||
    chr(8233)||chr(8239)||chr(8287)||chr(12288));
$$ LANGUAGE sql IMMUTABLE STRICT;

CREATE FUNCTION normalize_candidate_command(raw_body TEXT) RETURNS JSONB AS $$
DECLARE
  body JSONB;
  uuid_value TEXT;
  standard_uuid TEXT:='[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-' ||
                      '[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}';
BEGIN
  body:=raw_body::jsonb;
  IF (jsonb_typeof(body)='object'
    AND body ?& ARRAY['source_kind','source_id','source_sha256','expected_wait_version',
                     'idempotency_key','reason']
    AND (body-ARRAY['action','source_kind','source_id','source_sha256',
                   'expected_wait_version','idempotency_key','reason'])='{}'::jsonb
    AND (NOT body ? 'action' OR (jsonb_typeof(body->'action')='string'
                                AND body->>'action'='evaluate_candidate'))
    AND body->>'source_kind' IN ('terminal_handoff','human_route')
    AND jsonb_typeof(body->'source_id')='string'
    AND jsonb_typeof(body->'source_sha256')='string'
    AND jsonb_typeof(body->'idempotency_key')='string'
    AND jsonb_typeof(body->'reason')='string') IS NOT TRUE THEN RETURN NULL; END IF;
  IF body->>'source_kind'='terminal_handoff' THEN
    IF body->'expected_wait_version' IS DISTINCT FROM 'null'::jsonb THEN
      RETURN NULL;
    END IF;
  ELSIF (jsonb_typeof(body->'expected_wait_version')='number'
    AND raw_body::json->>'expected_wait_version' ~ '^[0-9]+$'
    AND body->'expected_wait_version' IN
        (SELECT to_jsonb(value) FROM generate_series(2,16) value)) IS NOT TRUE THEN
    RETURN NULL;
  END IF;
  uuid_value:=body->>'source_id';
  IF uuid_value ~ ('^urn:uuid:'||standard_uuid||'$') THEN
    uuid_value:=substring(uuid_value FROM 10);
  ELSIF uuid_value ~ ('^[{]'||standard_uuid||'[}]$') THEN
    uuid_value:=substring(uuid_value FROM 2 FOR char_length(uuid_value)-2);
  ELSIF uuid_value !~ ('^([0-9A-Fa-f]{32}|'||standard_uuid||')$') THEN
    RETURN NULL;
  END IF;
  RETURN jsonb_build_object(
    'action','evaluate_candidate','source_kind',body->>'source_kind',
    'source_id',uuid_value::uuid::text,
    'source_sha256',candidate_strip_text(body->>'source_sha256'),
    'expected_wait_version',body->'expected_wait_version',
    'idempotency_key',candidate_strip_text(body->>'idempotency_key'),
    'reason',candidate_strip_text(body->>'reason'));
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN
  RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

CREATE FUNCTION guard_candidate_evaluation_command() RETURNS trigger AS $$
DECLARE
  admission operator_authorizations%ROWTYPE;
  job investigation_candidate_evaluation_jobs%ROWTYPE;
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选评估命令审计不可更新或删除'; END IF;
  SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
  IF (admission.id IS NOT NULL AND admission.policy_version='operator-auth-v1'
    AND admission.role='engineer' AND admission.permissions ? 'manage_incident'
    AND admission.subject_id=NEW.subject_id AND admission.method='POST'
    AND admission.operation='evaluate_investigation_candidate'
    AND admission.route='/api/v1/investigations/{run_id}/candidate-evaluations'
    AND admission.resources=jsonb_build_object('run_id',NEW.investigation_run_id::text)
    AND admission.authorized_at<=NEW.created_at
    AND admission.body_sha256=encode(sha256(convert_to(NEW.raw_body,'UTF8')),'hex'))
      IS NOT TRUE THEN
    RAISE EXCEPTION '候选评估命令缺少精确原始正文或同源可信准入';
  END IF;
  IF (NEW.command->>'action'='evaluate_candidate'
    AND NEW.command->>'source_kind' IN ('terminal_handoff','human_route')
    AND (NEW.command->>'source_id') ~
        '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    AND NEW.command->>'source_sha256' ~ '^[0-9a-f]{64}$'
    AND ((NEW.command->>'source_kind'='terminal_handoff'
          AND NEW.command->'expected_wait_version'='null'::jsonb)
      OR (NEW.command->>'source_kind'='human_route'
          AND NEW.command->'expected_wait_version' IN
            (SELECT to_jsonb(value) FROM generate_series(2,16) value)))
    AND char_length(NEW.command->>'idempotency_key') BETWEEN 8 AND 100
    AND NEW.command->>'idempotency_key' ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'
    AND char_length(btrim(NEW.command->>'reason')) BETWEEN 1 AND 1000
    AND (NEW.command-ARRAY['action','source_kind','source_id','source_sha256',
        'expected_wait_version','idempotency_key','reason'])='{}'::jsonb
    AND NEW.command_sha256=encode(sha256(convert_to(
        candidate_canonical_json(NEW.command),'UTF8')),'hex')) IS NOT TRUE THEN
    RAISE EXCEPTION '候选评估命令缺少有界精确规范来源绑定';
  END IF;
  IF normalize_candidate_command(NEW.raw_body) IS DISTINCT FROM NEW.command THEN
    RAISE EXCEPTION '候选评估命令原始正文与规范命令不一致';
  END IF;
  IF NEW.outcome IN ('applied','replayed') THEN
    SELECT * INTO job FROM investigation_candidate_evaluation_jobs WHERE id=NEW.job_id;
    IF (job.id IS NOT NULL AND job.investigation_run_id=NEW.investigation_run_id
      AND job.requested_by_id=NEW.subject_id AND job.command_sha256=NEW.command_sha256
      AND job.source_kind=NEW.command->>'source_kind'
      AND job.source_id::text=NEW.command->>'source_id'
      AND job.source_sha256=NEW.command->>'source_sha256'
      AND COALESCE(to_jsonb(job.wait_version),'null'::jsonb)=NEW.command->'expected_wait_version'
      AND job.idempotency_key=NEW.command->>'idempotency_key'
      AND job.created_at<=NEW.created_at
      AND ((NEW.outcome='applied' AND job.authorization_id=NEW.authorization_id
            AND job.created_at=NEW.created_at)
        OR (NEW.outcome='replayed' AND job.authorization_id<>NEW.authorization_id))) IS NOT TRUE
  THEN
      RAISE EXCEPTION '候选评估命令结果与原始任务不一致';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_evaluation_commands_guard BEFORE INSERT OR UPDATE OR DELETE
  ON investigation_candidate_evaluation_commands FOR EACH ROW
  EXECUTE FUNCTION guard_candidate_evaluation_command();
"""


_ATTEMPT_GUARD = """

CREATE FUNCTION guard_candidate_evaluation_attempt() RETURNS trigger AS $$
DECLARE job investigation_candidate_evaluation_jobs%ROWTYPE;
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION '候选评估尝试不可删除'; END IF;
  IF TG_OP='UPDATE' AND
     (ROW(NEW.id,NEW.job_id,NEW.investigation_run_id,NEW.number,NEW.lease_token,
          NEW.worker_id,NEW.started_at,NEW.lease_expires_at) IS DISTINCT FROM
      ROW(OLD.id,OLD.job_id,OLD.investigation_run_id,OLD.number,OLD.lease_token,
          OLD.worker_id,OLD.started_at,OLD.lease_expires_at) OR OLD.status<>'running') THEN
    RAISE EXCEPTION '候选评估尝试身份或终态不可改写';
  END IF;
  IF TG_OP='INSERT' AND NEW.status<>'running' THEN
    RAISE EXCEPTION '候选评估尝试必须从运行状态开始';
  END IF;
  IF TG_OP='INSERT' THEN
    SELECT * INTO job FROM investigation_candidate_evaluation_jobs WHERE id=NEW.job_id;
    IF job.id IS NULL OR NEW.started_at<job.created_at
      OR NEW.started_at>=(job.source->>'started_at')::timestamptz+
         make_interval(secs=>(job.source #>> '{call_budget,max_duration_seconds}')::int) THEN
      RAISE EXCEPTION '候选评估尝试需要原时限内且不早于来源的独立任务';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_evaluation_attempts_guard BEFORE INSERT OR UPDATE OR DELETE
  ON investigation_candidate_evaluation_attempts FOR EACH ROW EXECUTE FUNCTION
  guard_candidate_evaluation_attempt();

"""

_CALL_GUARD = """

CREATE FUNCTION guard_candidate_evaluation_call() RETURNS trigger AS $$
DECLARE
  attempt investigation_candidate_evaluation_attempts%ROWTYPE;
  job investigation_candidate_evaluation_jobs%ROWTYPE;
  handoff investigation_terminal_handoffs%ROWTYPE;
  run investigation_runs%ROWTYPE;
  model_count INTEGER;
  input_occupied NUMERIC;
  output_occupied NUMERIC;
  cost_occupied NUMERIC;
  input_price NUMERIC;
  output_price NUMERIC;
BEGIN
  IF TG_OP='DELETE' THEN
    IF OLD.operation='evaluate_candidate' THEN RAISE EXCEPTION '候选评估调用不可删除'; END IF;
    RETURN OLD;
  END IF;
  IF NEW.operation<>'evaluate_candidate' THEN
    IF TG_OP='UPDATE' AND OLD.operation='evaluate_candidate' THEN
      RAISE EXCEPTION '候选评估调用操作不可改写';
    END IF;
    RETURN NEW;
  END IF;
  PERFORM 1 FROM work_items WHERE investigation_run_id=NEW.investigation_run_id FOR UPDATE;
  IF TG_OP='UPDATE' THEN
    IF OLD.operation<>'evaluate_candidate' OR OLD.status<>'reserved'
      OR ROW(NEW.id,NEW.investigation_run_id,NEW.investigation_attempt_id,
          NEW.candidate_attempt_id,NEW.task_id,NEW.task_attempt_id,NEW.sequence,
          NEW.invocation_index,NEW.idempotency_key,NEW.operation,NEW.kind,NEW.reserved_at,
          NEW.model_input,NEW.model_allocation,NEW.context_manifest,NEW.tool_request)
        IS DISTINCT FROM
         ROW(OLD.id,OLD.investigation_run_id,OLD.investigation_attempt_id,
          OLD.candidate_attempt_id,OLD.task_id,OLD.task_attempt_id,OLD.sequence,
          OLD.invocation_index,OLD.idempotency_key,OLD.operation,OLD.kind,OLD.reserved_at,
          OLD.model_input,OLD.model_allocation,OLD.context_manifest,OLD.tool_request)
      OR NEW.status='reserved' THEN
      RAISE EXCEPTION '候选评估调用只允许预留到一次不可变终态';
    END IF;
  END IF;
  SELECT * INTO attempt FROM investigation_candidate_evaluation_attempts
    WHERE id=NEW.candidate_attempt_id;
  SELECT * INTO job FROM investigation_candidate_evaluation_jobs WHERE id=attempt.job_id;
  SELECT * INTO handoff FROM investigation_terminal_handoffs
    WHERE id=(job.source #>> '{handoff,id}')::uuid;
  SELECT * INTO run FROM investigation_runs WHERE id=NEW.investigation_run_id;
  IF attempt.id IS NULL OR job.id IS NULL OR handoff.id IS NULL
    OR attempt.investigation_run_id IS DISTINCT FROM NEW.investigation_run_id
    OR job.investigation_run_id IS DISTINCT FROM NEW.investigation_run_id
    OR handoff.investigation_run_id IS DISTINCT FROM NEW.investigation_run_id
    OR handoff.investigation_attempt_id IS DISTINCT FROM NEW.investigation_attempt_id
    OR NEW.model_input IS DISTINCT FROM job.packet->'model_input'
    OR NEW.model_allocation IS DISTINCT FROM job.packet->'allocation'
    OR NEW.context_manifest IS DISTINCT FROM job.packet->'manifest'
    OR NEW.model_input IS NULL OR NEW.model_allocation IS NULL
    OR NEW.model_input='null'::jsonb OR NEW.model_allocation='null'::jsonb
    OR NEW.human_review_attempt_id IS NOT NULL THEN
    RAISE EXCEPTION '候选评估调用缺少真实执行归属或精确冻结输入';
  END IF;
  -- 失联收尾不授予执行资格，允许已取消或过期旧尝试仅结算 unknown。
  IF TG_OP='UPDATE' AND NEW.status='unknown' THEN
    IF NEW.call_result IS NOT NULL THEN
      RAISE EXCEPTION '未知候选评估调用不能保存成功意见';
    END IF;
    RETURN NEW;
  END IF;
  IF attempt.status<>'running' OR job.status<>'running' OR attempt.number<>job.attempt_count
    OR attempt.lease_token IS DISTINCT FROM job.lease_token
    OR attempt.worker_id IS DISTINCT FROM job.locked_by
    OR attempt.lease_expires_at IS DISTINCT FROM job.locked_until
    OR NEW.reserved_at<attempt.started_at OR NEW.reserved_at>=attempt.lease_expires_at
    OR (TG_OP='UPDATE' AND NEW.finished_at>=attempt.lease_expires_at) THEN
    RAISE EXCEPTION '候选评估调用需要当前有效新阶段租约';
  END IF;
  IF TG_OP='UPDATE' AND NEW.status='failed' THEN
    IF NEW.call_result IS NOT NULL THEN RAISE EXCEPTION '失败候选评估调用不能保存意见'; END IF;
    RETURN NEW;
  END IF;
  PERFORM assert_candidate_source(job,TRUE);
  IF TG_OP='UPDATE' THEN
    IF NEW.status<>'succeeded' OR NEW.call_result IS NULL
      OR NEW.finished_at>=run.started_at +
         make_interval(secs=>(run.call_budget->>'max_duration_seconds')::int) THEN
      RAISE EXCEPTION '候选评估成功调用必须保存结构化意见';
    END IF;
    PERFORM assert_candidate_proposal(NEW.call_result->'proposal',job.source);
    RETURN NEW;
  END IF;
  IF NEW.status<>'reserved' OR NEW.reserved_at>=run.started_at +
       make_interval(secs=>(run.call_budget->>'max_duration_seconds')::int) THEN
    RAISE EXCEPTION '候选评估预留不能跳过原运行时限';
  END IF;
  SELECT count(*) INTO model_count FROM investigation_call_reservations
    WHERE investigation_run_id=NEW.investigation_run_id AND kind='model';
  IF model_count>=(run.call_budget->>'max_model_calls')::int
    OR NEW.sequence<>(SELECT count(*)+1 FROM investigation_call_reservations
                     WHERE investigation_run_id=NEW.investigation_run_id)
    OR EXISTS (SELECT 1 FROM investigation_call_reservations
               WHERE investigation_run_id=NEW.investigation_run_id AND status='reserved') THEN
    RAISE EXCEPTION '候选评估预留超过原调用次数或存在未结算调用';
  END IF;
  IF EXISTS (SELECT 1 FROM investigation_call_reservations c
      WHERE c.investigation_run_id=run.id AND c.kind='model'
      AND ((c.model_usage->>'input_tokens')::numeric>
             (c.model_allocation->>'estimated_input_tokens')::numeric
        OR (c.model_usage->>'output_tokens')::numeric>
             (c.model_allocation->>'output_token_limit')::numeric)) THEN
    RAISE EXCEPTION '候选评估不能继续使用超过原分配的模型调用';
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
    RAISE EXCEPTION '候选评估预留超过原累计模型预算';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_evaluation_calls_guard BEFORE INSERT OR UPDATE OR DELETE
  ON investigation_call_reservations FOR EACH ROW EXECUTE FUNCTION
  guard_candidate_evaluation_call();

"""

_RESULT_GUARD = """

CREATE FUNCTION guard_candidate_evaluation_result() RETURNS trigger AS $$
DECLARE
  job investigation_candidate_evaluation_jobs%ROWTYPE;
  attempt investigation_candidate_evaluation_attempts%ROWTYPE;
  caller investigation_candidate_evaluation_attempts%ROWTYPE;
  source_call investigation_call_reservations%ROWTYPE;
  run investigation_runs%ROWTYPE;
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '候选评估意见不可更新或删除'; END IF;
  SELECT * INTO job FROM investigation_candidate_evaluation_jobs WHERE id=NEW.job_id;
  IF job.id IS NULL THEN RAISE EXCEPTION '候选评估意见缺少任务'; END IF;
  PERFORM 1 FROM work_items WHERE investigation_run_id=job.investigation_run_id FOR UPDATE;
  SELECT * INTO job FROM investigation_candidate_evaluation_jobs WHERE id=NEW.job_id FOR UPDATE;
  SELECT * INTO attempt FROM investigation_candidate_evaluation_attempts WHERE id=NEW.attempt_id;
  SELECT * INTO source_call FROM investigation_call_reservations WHERE id=NEW.source_call_id;
  SELECT * INTO caller FROM investigation_candidate_evaluation_attempts
    WHERE id=source_call.candidate_attempt_id;
  SELECT * INTO run FROM investigation_runs WHERE id=job.investigation_run_id;
  PERFORM assert_candidate_source(job,TRUE);
  PERFORM assert_candidate_step(NEW.content,job.source);
  IF job.status<>'running' OR attempt.id IS NULL OR attempt.job_id IS DISTINCT FROM job.id
    OR attempt.status<>'running' OR attempt.number<>job.attempt_count
    OR attempt.lease_token IS DISTINCT FROM job.lease_token
    OR attempt.lease_expires_at IS DISTINCT FROM job.locked_until
    OR NEW.created_at<attempt.started_at OR NEW.created_at>=attempt.lease_expires_at
    OR source_call.id IS NULL OR source_call.operation<>'evaluate_candidate'
    OR source_call.status<>'succeeded' OR caller.job_id IS DISTINCT FROM job.id
    OR source_call.call_result->'proposal' IS DISTINCT FROM NEW.content->'proposal'
    OR NEW.content->'execution_eligible' IS DISTINCT FROM 'false'::jsonb
    OR NEW.content->>'version' IS DISTINCT FROM 'candidate-evaluation-result-v1'
    OR NEW.content->>'disposition' NOT IN ('planning_review','await_human')
    OR COALESCE(NEW.content->>'reason_code','') !~ '^[a-z0-9_]{1,100}$'
    OR source_call.finished_at>NEW.created_at
    OR EXISTS (SELECT 1 FROM investigation_call_reservations c
        WHERE c.investigation_run_id=job.investigation_run_id AND c.kind='model'
        AND ((c.model_usage->>'input_tokens')::numeric>
               (c.model_allocation->>'estimated_input_tokens')::numeric
          OR (c.model_usage->>'output_tokens')::numeric>
               (c.model_allocation->>'output_token_limit')::numeric))
    OR NEW.created_at>=run.started_at +
       make_interval(secs=>(run.call_budget->>'max_duration_seconds')::int) THEN
    RAISE EXCEPTION '候选评估意见需要当前租约及同任务成功调用';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER candidate_evaluation_results_guard BEFORE INSERT OR UPDATE OR DELETE
  ON investigation_candidate_evaluation_results FOR EACH ROW EXECUTE FUNCTION
  guard_candidate_evaluation_result();

"""

_STATE_GUARD = """

CREATE FUNCTION validate_candidate_evaluation_state() RETURNS trigger AS $$
DECLARE
  target UUID;
  job investigation_candidate_evaluation_jobs%ROWTYPE;
  attempt investigation_candidate_evaluation_attempts%ROWTYPE;
  result investigation_candidate_evaluation_results%ROWTYPE;
  audit investigation_candidate_evaluation_commands%ROWTYPE;
  attempts INTEGER;
BEGIN
  IF TG_TABLE_NAME='investigation_candidate_evaluation_jobs' THEN target:=NEW.id;
  ELSE target:=NEW.job_id; END IF;
  SELECT * INTO job FROM investigation_candidate_evaluation_jobs WHERE id=target;
  SELECT * INTO audit FROM investigation_candidate_evaluation_commands
    WHERE authorization_id=job.authorization_id;
  SELECT count(*) INTO attempts FROM investigation_candidate_evaluation_attempts WHERE
  job_id=job.id;
  SELECT * INTO attempt FROM investigation_candidate_evaluation_attempts
    WHERE job_id=job.id ORDER BY number DESC LIMIT 1;
  SELECT * INTO result FROM investigation_candidate_evaluation_results WHERE job_id=job.id;
  IF job.id IS NULL OR audit.authorization_id IS NULL OR audit.outcome<>'applied'
    OR audit.job_id IS DISTINCT FROM job.id OR audit.created_at IS DISTINCT FROM job.created_at
    OR audit.subject_id IS DISTINCT FROM job.requested_by_id
    OR audit.command_sha256 IS DISTINCT FROM job.command_sha256
    OR attempts<>job.attempt_count
    OR (attempt.id IS NOT NULL AND attempt.number<>job.attempt_count)
    OR EXISTS (SELECT 1 FROM investigation_candidate_evaluation_attempts a WHERE a.job_id=job.id
               AND a.number<job.attempt_count AND a.status IN ('running','succeeded')) THEN
    RAISE EXCEPTION '候选评估任务缺少同源成功命令或连续尝试';
  END IF;
  IF job.status='running' THEN
    IF attempt.id IS NULL OR attempt.status<>'running'
      OR attempt.lease_token IS DISTINCT FROM job.lease_token
      OR attempt.worker_id IS DISTINCT FROM job.locked_by
      OR attempt.lease_expires_at IS DISTINCT FROM job.locked_until
      OR result.job_id IS NOT NULL THEN
      RAISE EXCEPTION '候选评估任务与当前租约尝试不一致';
    END IF;
  ELSIF job.status='succeeded' THEN
    IF attempt.id IS NULL OR attempt.status<>'succeeded' OR result.job_id IS NULL
      OR result.attempt_id IS DISTINCT FROM attempt.id
      OR result.created_at IS DISTINCT FROM job.completed_at
      OR attempt.finished_at IS DISTINCT FROM result.created_at THEN
      RAISE EXCEPTION '候选评估成功需要同尝试和同时间的独立意见';
    END IF;
  ELSIF result.job_id IS NOT NULL OR attempt.status IN ('running','succeeded') THEN
    RAISE EXCEPTION '未成功候选评估不能保留活动尝试或意见';
  END IF;
  IF job.status<>'running' AND EXISTS (
     SELECT 1 FROM investigation_call_reservations c
       JOIN investigation_candidate_evaluation_attempts a ON a.id=c.candidate_attempt_id
       WHERE a.job_id=job.id AND c.status='reserved') THEN
    RAISE EXCEPTION '候选评估任务收尾不能遗留未结算预留';
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER candidate_evaluation_jobs_state AFTER INSERT OR UPDATE
  ON investigation_candidate_evaluation_jobs DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION validate_candidate_evaluation_state();
CREATE CONSTRAINT TRIGGER candidate_evaluation_attempts_state AFTER INSERT OR UPDATE
  ON investigation_candidate_evaluation_attempts DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION validate_candidate_evaluation_state();
CREATE CONSTRAINT TRIGGER candidate_evaluation_results_state AFTER INSERT
  ON investigation_candidate_evaluation_results DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION validate_candidate_evaluation_state();
"""

_GUARDS = _JOB_GUARD + _ATTEMPT_GUARD + _COMMAND_GUARD + _CALL_GUARD + _RESULT_GUARD + _STATE_GUARD


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_candidate_evaluation_jobs)
          OR EXISTS (SELECT 1 FROM investigation_candidate_evaluation_attempts)
          OR EXISTS (SELECT 1 FROM investigation_candidate_evaluation_results)
          OR EXISTS (SELECT 1 FROM investigation_candidate_evaluation_commands)
          OR EXISTS (SELECT 1 FROM investigation_call_reservations
                     WHERE operation='evaluate_candidate' OR candidate_attempt_id IS NOT NULL) THEN
          RAISE EXCEPTION '不能删除已保存的候选评估事实或预算调用';
        END IF;
      END $$;
      DROP TRIGGER candidate_evaluation_calls_guard ON investigation_call_reservations;
      DROP FUNCTION guard_candidate_evaluation_call();
      ALTER TABLE investigation_call_reservations
        DROP CONSTRAINT fk_call_reservations_candidate_attempt,
        DROP CONSTRAINT ck_call_reservations_candidate_owner;
    """)
    _constraints(_OLD_CONSTRAINTS)
    op.execute("""
      ALTER TABLE investigation_call_reservations DROP COLUMN candidate_attempt_id;
      DROP TRIGGER candidate_evaluation_jobs_state ON investigation_candidate_evaluation_jobs;
      DROP TRIGGER candidate_evaluation_attempts_state
        ON investigation_candidate_evaluation_attempts;
      DROP TRIGGER candidate_evaluation_results_state ON investigation_candidate_evaluation_results;
      DROP FUNCTION validate_candidate_evaluation_state();
      DROP FUNCTION assert_candidate_step(JSONB,JSONB);
      DROP FUNCTION assert_candidate_proposal(JSONB,JSONB);
      DROP FUNCTION assert_candidate_source(investigation_candidate_evaluation_jobs,BOOLEAN);
      DROP TABLE investigation_candidate_evaluation_commands;
      DROP TABLE investigation_candidate_evaluation_results;
      DROP TABLE investigation_candidate_evaluation_attempts;
      DROP TABLE investigation_candidate_evaluation_jobs;
      DROP FUNCTION guard_candidate_evaluation_command();
      DROP FUNCTION normalize_candidate_command(TEXT);
      DROP FUNCTION candidate_strip_text(TEXT);
      DROP FUNCTION guard_candidate_evaluation_result();
      DROP FUNCTION guard_candidate_evaluation_attempt();
      DROP FUNCTION guard_candidate_evaluation_job();
      DROP FUNCTION candidate_canonical_json(JSONB);
    """)
