"""保存成功人工重评的不可变、未分派后续路由。"""

from alembic import op

revision = "0071_human_reassessment_routing"
down_revision = "0070_human_input_reassessment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE investigation_human_review_routes (
  id UUID PRIMARY KEY,
  job_id UUID NOT NULL REFERENCES investigation_human_review_results(job_id) ON DELETE RESTRICT,
  investigation_run_id UUID NOT NULL,
  wait_version INTEGER NOT NULL,
  preparation_source_sha256 VARCHAR(64) NOT NULL,
  result_sha256 VARCHAR(64) NOT NULL,
  source_call_id UUID NOT NULL REFERENCES investigation_call_reservations(id) ON DELETE RESTRICT,
  decision JSONB NOT NULL,
  content_sha256 VARCHAR(64) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  CONSTRAINT uq_human_review_routes_job UNIQUE (job_id),
  CONSTRAINT fk_human_review_routes_job_run FOREIGN KEY (job_id,investigation_run_id)
    REFERENCES investigation_human_review_jobs(id,investigation_run_id) ON DELETE RESTRICT,
  CONSTRAINT ck_human_review_routes_wait CHECK (wait_version BETWEEN 2 AND 16),
  CONSTRAINT ck_human_review_routes_hashes CHECK (
    preparation_source_sha256 ~ '^[0-9a-f]{64}$' AND result_sha256 ~ '^[0-9a-f]{64}$'
    AND content_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_human_review_routes_content CHECK (
    jsonb_typeof(decision)='object' AND octet_length(decision::text)<=1048576)
);
CREATE FUNCTION guard_human_review_route() RETURNS trigger AS $$
DECLARE
  job investigation_human_review_jobs%ROWTYPE;
  result investigation_human_review_results%ROWTYPE;
  preparation investigation_human_preparation_jobs%ROWTYPE;
  run investigation_runs%ROWTYPE;
  original JSONB;
  base JSONB;
  deadline TIMESTAMPTZ;
  current_version INTEGER;
  model_count INTEGER;
  tool_count INTEGER;
  call_count INTEGER;
BEGIN
  IF TG_OP<>'INSERT' THEN
    RAISE EXCEPTION '人工重评后续路由不可改写或删除';
  END IF;
  PERFORM 1 FROM work_items WHERE investigation_run_id=NEW.investigation_run_id FOR UPDATE;
  SELECT * INTO job FROM investigation_human_review_jobs WHERE id=NEW.job_id;
  SELECT * INTO result FROM investigation_human_review_results WHERE job_id=NEW.job_id;
  SELECT * INTO preparation FROM investigation_human_preparation_jobs
    WHERE id=job.preparation_job_id;
  SELECT * INTO run FROM investigation_runs WHERE id=NEW.investigation_run_id;
  PERFORM 1 FROM incidents WHERE id=run.incident_id FOR UPDATE;
  SELECT max(state_version) INTO current_version FROM investigation_human_wait_events
    WHERE investigation_run_id=NEW.investigation_run_id;
  deadline := run.started_at +
    (run.call_budget->>'max_duration_seconds')::INTEGER * interval '1 second';
  original := preparation.source #> '{route,decision}';
  base := NEW.decision->'base_decision';
  IF job.id IS NULL OR result.job_id IS NULL OR preparation.id IS NULL
    OR run.status IS DISTINCT FROM 'await_human'
    OR NOT EXISTS (SELECT 1 FROM incidents WHERE id=run.incident_id AND status='investigating')
    OR job.status IS DISTINCT FROM 'succeeded'
    OR job.investigation_run_id IS DISTINCT FROM NEW.investigation_run_id
    OR job.wait_version IS DISTINCT FROM NEW.wait_version
    OR current_version IS DISTINCT FROM NEW.wait_version
    OR EXISTS (SELECT 1 FROM investigation_human_wait_events
      WHERE investigation_run_id=run.id AND action='cancel')
    OR preparation.source_sha256 IS DISTINCT FROM NEW.preparation_source_sha256
    OR result.content_sha256 IS DISTINCT FROM NEW.result_sha256
    OR result.source_call_id IS DISTINCT FROM NEW.source_call_id
    OR NEW.created_at<result.created_at
    OR NEW.decision->>'version' IS DISTINCT FROM 'human-review-routing-v1'
    OR NEW.decision->>'dispatch_status' IS DISTINCT FROM 'not_dispatched'
    OR COALESCE(NEW.decision->>'route','') NOT IN ('await_human','follow_up','candidate_evaluation')
    OR NEW.decision #> '{budget,policy}' IS DISTINCT FROM run.call_budget
    OR NEW.decision #> '{budget,model_budget}' IS DISTINCT FROM run.model_budget
    OR (NEW.decision #>> '{budget,started_at}')::timestamptz IS DISTINCT FROM run.started_at
    OR (NEW.decision #>> '{budget,deadline}')::timestamptz IS DISTINCT FROM deadline
    OR NEW.decision #>> '{budget,investigation_run_id}' IS DISTINCT FROM run.id::text
    OR base->'round_number' IS DISTINCT FROM original->'round_number'
    OR base->'existing_task_count' IS DISTINCT FROM original->'existing_task_count'
    OR base->'max_rounds' IS DISTINCT FROM original->'max_rounds'
    OR base->'max_tasks_per_round' IS DISTINCT FROM original->'max_tasks_per_round'
    OR base->'max_total_tasks' IS DISTINCT FROM original->'max_total_tasks'
    OR base->'remaining_rounds' IS DISTINCT FROM original->'remaining_rounds'
    OR base->'remaining_task_slots' IS DISTINCT FROM original->'remaining_task_slots'
    OR COALESCE(base->>'route','') NOT IN ('await_human','follow_up','candidate_evaluation')
    OR jsonb_typeof(base->'follow_up_suggestions') IS DISTINCT FROM 'array'
    OR COALESCE(NEW.decision->>'minimum_model_calls','') !~ '^[0-5]$'
    OR COALESCE(NEW.decision->>'minimum_tool_calls','') !~ '^[0-3]$'
    OR (NEW.decision->>'minimum_model_calls')::INTEGER IS DISTINCT FROM
      (CASE base->>'route' WHEN 'follow_up' THEN
        jsonb_array_length(base->'follow_up_suggestions')+2
        WHEN 'candidate_evaluation' THEN 1 ELSE 0 END)
    OR (NEW.decision->>'minimum_tool_calls')::INTEGER IS DISTINCT FROM
      (CASE base->>'route' WHEN 'follow_up' THEN
        jsonb_array_length(base->'follow_up_suggestions') ELSE 0 END)
    OR EXISTS (SELECT 1 FROM investigation_call_reservations
      WHERE investigation_run_id=run.id AND status='reserved') THEN
    RAISE EXCEPTION '路由必须绑定当前输入、完整成功重评与原预算来源';
  END IF;
  SELECT count(*) FILTER (WHERE kind='model'), count(*) FILTER (WHERE kind='tool'), count(*)
    INTO model_count, tool_count, call_count FROM investigation_call_reservations
    WHERE investigation_run_id=run.id;
  IF (NEW.decision #>> '{budget,reserved_model_calls}')::INTEGER IS DISTINCT FROM model_count
    OR (NEW.decision #>> '{budget,reserved_tool_calls}')::INTEGER IS DISTINCT FROM tool_count
    OR jsonb_array_length(NEW.decision #> '{budget,calls}') IS DISTINCT FROM call_count THEN
    RAISE EXCEPTION '路由预算快照不能遗漏共享调用';
  END IF;
  IF NEW.decision->>'route'<>'await_human' AND (
    NEW.created_at>=deadline
    OR NEW.decision->>'route' IS DISTINCT FROM base->>'route'
    OR NEW.decision->'blocking_reason_codes' IS DISTINCT FROM '[]'::jsonb
    OR NEW.decision->'unresolved_constraints' IS DISTINCT FROM '[]'::jsonb
    OR EXISTS (SELECT 1 FROM jsonb_array_elements(preparation.source #> '{wait,events}') e,
      jsonb_array_elements(e #> '{command,items}') item WHERE item->>'kind'='constraint')
    OR model_count+(NEW.decision->>'minimum_model_calls')::INTEGER
      >(run.call_budget->>'max_model_calls')::INTEGER
    OR tool_count+(NEW.decision->>'minimum_tool_calls')::INTEGER
      >(run.call_budget->>'max_tool_calls')::INTEGER
    OR (NEW.decision->>'route'='follow_up' AND (base->>'remaining_rounds')::INTEGER<1)
  ) THEN
    RAISE EXCEPTION '自动意图不得绕过原截止时间、人工约束或容量';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER human_review_routes_guard BEFORE INSERT OR UPDATE OR DELETE
  ON investigation_human_review_routes FOR EACH ROW EXECUTE FUNCTION guard_human_review_route();
""")


def downgrade() -> None:
    op.execute("""
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM investigation_human_review_routes) THEN
    RAISE EXCEPTION '不能删除已保存的人工重评路由事实';
  END IF;
END $$;
DROP TABLE investigation_human_review_routes;
DROP FUNCTION guard_human_review_route();
""")
