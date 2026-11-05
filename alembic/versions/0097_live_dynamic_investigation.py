"""固定现场动态图来源、精确任务窗口和安全终端交接。"""

from alembic import op

revision = "0097_live_dynamic_investigation"
down_revision = "0096_reflow_approval_decision"
branch_labels = None
depends_on = None


def _replace_constraints(constraints: dict[str, str]) -> None:
    for name, expression in constraints.items():
        op.execute(f"ALTER TABLE investigation_runs DROP CONSTRAINT {name}")
        op.execute(f"ALTER TABLE investigation_runs ADD CONSTRAINT {name} CHECK ({expression})")


def upgrade() -> None:
    op.execute(
        "ALTER TABLE investigation_runs ALTER COLUMN evidence_policy_version TYPE VARCHAR(40)"
    )
    _replace_constraints(_LIVE_CONSTRAINTS)
    op.execute(_LIVE_HANDOFF_INSERT)
    op.execute(_LIVE_SOURCE_GUARDS)


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_runs
                   WHERE graph_version = 'dynamic-investigation-graph-v16'
                     OR evidence_policy_version = 'task-evidence-live-dynamic-mcp-v1') THEN
          RAISE EXCEPTION '不能降级已存在的现场动态图运行与来源事实';
        END IF;
      END $$;
      DROP TRIGGER live_dynamic_source_immutable ON investigation_runs;
      DROP FUNCTION protect_live_dynamic_source();
      DROP TRIGGER live_dynamic_task_scope ON agent_tasks;
      DROP FUNCTION guard_live_dynamic_task();
      DROP TRIGGER live_dynamic_evidence_source ON agent_task_evidence;
      DROP FUNCTION guard_live_dynamic_evidence();
      DROP FUNCTION assert_live_dynamic_assignment(investigation_runs,JSONB);
    """)
    op.execute(_PREVIOUS_HANDOFF_INSERT)
    _replace_constraints(_PREVIOUS_CONSTRAINTS)
    op.execute(
        "ALTER TABLE investigation_runs ALTER COLUMN evidence_policy_version TYPE VARCHAR(32)"
    )


_PREVIOUS_CONSTRAINTS = {
    "ck_investigation_runs_observation_cutoff": (
        "(workflow_version = 'v2' AND data_source_mode = 'live' AND observation_cutoff_at IS "
        "NOT NULL AND evidence_policy_version IS NOT NULL AND evidence_policy_version IN "
        "('task-evidence-live-v1', 'task-evidence-live-mcp-log-v1', "
        "'task-evidence-live-mcp-v1') AND observation_cutoff_at = date_trunc('second', "
        "observation_cutoff_at) AND observation_cutoff_at <= created_at AND "
        "observation_cutoff_at >= TIMESTAMPTZ '1970-01-01 06:00:00+00') OR "
        "((workflow_version <> 'v2' OR data_source_mode <> 'live') AND observation_cutoff_at "
        "IS NULL)"
    ),
    "ck_investigation_runs_evidence_policy": (
        "evidence_policy_version IS NULL OR (workflow_version IN ('v2', 'v3') AND "
        "call_budget IS NOT NULL AND ((evidence_policy_version = 'task-evidence-v1' AND "
        "data_source_mode = 'frozen') OR (evidence_policy_version IN "
        "('task-evidence-live-v1', 'task-evidence-live-mcp-log-v1', "
        "'task-evidence-live-mcp-v1') AND data_source_mode = 'live')))"
    ),
    "ck_investigation_runs_dynamic_policy": (
        "(workflow_version <> 'v3' AND graph_version IS NULL AND coordination_policy_version "
        "IS NULL) OR (workflow_version = 'v3' AND data_source_mode = 'frozen' AND "
        "COALESCE(graph_version, '') IN ('dynamic-investigation-graph-v1', "
        "'dynamic-investigation-graph-v2', 'dynamic-investigation-graph-v3', "
        "'dynamic-investigation-graph-v4', 'dynamic-investigation-graph-v5', "
        "'dynamic-investigation-graph-v6', 'dynamic-investigation-graph-v7', "
        "'dynamic-investigation-graph-v8', 'dynamic-investigation-graph-v9', "
        "'dynamic-investigation-graph-v10', 'dynamic-investigation-graph-v11', "
        "'dynamic-investigation-graph-v12', 'dynamic-investigation-graph-v13', "
        "'dynamic-investigation-graph-v14', 'dynamic-investigation-graph-v15') AND "
        "COALESCE(coordination_policy_version, '') = 'dynamic-coordination-v1' AND "
        "call_budget IS NOT NULL AND model_budget IS NOT NULL AND "
        "COALESCE(context_policy_version, '') = 'summary-context-v1' AND "
        "COALESCE(working_summary_policy_version, '') = 'task-working-summary-v1' AND "
        "COALESCE(evidence_policy_version, '') = 'task-evidence-v1' AND prompt_version = "
        "'summary-context-v1' AND COALESCE(model_budget #>> "
        "'{profile,structured_output_method}', '') = 'function_calling')"
    ),
    "ck_investigation_runs_coordination_experience_policy": (
        "coordination_experience_policy_version IS NULL OR "
        "(coordination_experience_policy_version = 'coordination-experience-v1' AND "
        "workflow_version = 'v3' AND graph_version = 'dynamic-investigation-graph-v15' AND "
        "data_source_mode = 'frozen' AND context_policy_version = 'summary-context-v1' AND "
        "working_summary_policy_version = 'task-working-summary-v1' AND "
        "evidence_policy_version = 'task-evidence-v1' AND prompt_version = "
        "'summary-context-v1' AND model_budget IS NOT NULL AND COALESCE(model_budget #>> "
        "'{profile,structured_output_method}', '') = 'function_calling')"
    ),
    "ck_investigation_runs_handoff_scope": (
        "status NOT IN ('await_human', 'await_candidate') OR (workflow_version = 'v3' AND "
        "graph_version = 'dynamic-investigation-graph-v15' AND data_source_mode = 'frozen')"
    ),
}

_LIVE_CONSTRAINTS = {
    "ck_investigation_runs_observation_cutoff": (
        "(workflow_version IN ('v2', 'v3') AND data_source_mode = 'live' AND "
        "observation_cutoff_at IS NOT NULL AND evidence_policy_version IS NOT NULL AND "
        "evidence_policy_version IN ('task-evidence-live-v1', "
        "'task-evidence-live-mcp-log-v1', 'task-evidence-live-mcp-v1', "
        "'task-evidence-live-dynamic-mcp-v1') AND observation_cutoff_at = "
        "date_trunc('second', observation_cutoff_at) AND observation_cutoff_at <= created_at "
        "AND observation_cutoff_at >= TIMESTAMPTZ '1970-01-01 06:00:00+00') OR "
        "((workflow_version NOT IN ('v2', 'v3') OR data_source_mode <> 'live') AND "
        "observation_cutoff_at IS NULL)"
    ),
    "ck_investigation_runs_evidence_policy": (
        "((evidence_policy_version IS NULL OR (workflow_version IN ('v2', 'v3') AND "
        "call_budget IS NOT NULL AND ((evidence_policy_version = 'task-evidence-v1' AND "
        "data_source_mode = 'frozen') OR (evidence_policy_version IN "
        "('task-evidence-live-v1', 'task-evidence-live-mcp-log-v1', "
        "'task-evidence-live-mcp-v1') AND data_source_mode = 'live')))) OR (workflow_version "
        "= 'v3' AND COALESCE(graph_version, '') = 'dynamic-investigation-graph-v16' AND "
        "data_source_mode = 'live' AND COALESCE(evidence_policy_version, '') = "
        "'task-evidence-live-dynamic-mcp-v1' AND call_budget IS NOT NULL)) AND "
        "(evidence_policy_version IS DISTINCT FROM 'task-evidence-live-dynamic-mcp-v1' OR "
        "(workflow_version = 'v3' AND COALESCE(graph_version, '') = "
        "'dynamic-investigation-graph-v16' AND data_source_mode = 'live' AND "
        "COALESCE(evidence_policy_version, '') = 'task-evidence-live-dynamic-mcp-v1'))"
    ),
    "ck_investigation_runs_dynamic_policy": (
        "((workflow_version <> 'v3' AND graph_version IS NULL AND "
        "coordination_policy_version IS NULL) OR (workflow_version = 'v3' AND "
        "data_source_mode = 'frozen' AND COALESCE(graph_version, '') IN "
        "('dynamic-investigation-graph-v1', 'dynamic-investigation-graph-v2', "
        "'dynamic-investigation-graph-v3', 'dynamic-investigation-graph-v4', "
        "'dynamic-investigation-graph-v5', 'dynamic-investigation-graph-v6', "
        "'dynamic-investigation-graph-v7', 'dynamic-investigation-graph-v8', "
        "'dynamic-investigation-graph-v9', 'dynamic-investigation-graph-v10', "
        "'dynamic-investigation-graph-v11', 'dynamic-investigation-graph-v12', "
        "'dynamic-investigation-graph-v13', 'dynamic-investigation-graph-v14', "
        "'dynamic-investigation-graph-v15') AND COALESCE(coordination_policy_version, '') = "
        "'dynamic-coordination-v1' AND call_budget IS NOT NULL AND model_budget IS NOT NULL "
        "AND COALESCE(context_policy_version, '') = 'summary-context-v1' AND "
        "COALESCE(working_summary_policy_version, '') = 'task-working-summary-v1' AND "
        "COALESCE(evidence_policy_version, '') = 'task-evidence-v1' AND prompt_version = "
        "'summary-context-v1' AND COALESCE(model_budget #>> "
        "'{profile,structured_output_method}', '') = 'function_calling')) OR "
        "(workflow_version = 'v3' AND COALESCE(graph_version, '') = "
        "'dynamic-investigation-graph-v16' AND data_source_mode = 'live' AND "
        "COALESCE(evidence_policy_version, '') = 'task-evidence-live-dynamic-mcp-v1' AND "
        "status IN ('queued', 'running', 'failed', 'await_human', 'await_candidate') AND "
        "COALESCE(coordination_policy_version, '') = 'dynamic-coordination-v1' AND "
        "call_budget IS NOT NULL AND model_budget IS NOT NULL AND "
        "COALESCE(context_policy_version, '') = 'summary-context-v1' AND "
        "COALESCE(working_summary_policy_version, '') = 'task-working-summary-v1' AND "
        "prompt_version = 'summary-context-v1' AND COALESCE(model_budget #>> "
        "'{profile,structured_output_method}', '') = 'function_calling')"
    ),
    "ck_investigation_runs_coordination_experience_policy": (
        "(coordination_experience_policy_version IS NULL OR "
        "(coordination_experience_policy_version = 'coordination-experience-v1' AND "
        "workflow_version = 'v3' AND graph_version = 'dynamic-investigation-graph-v15' AND "
        "data_source_mode = 'frozen' AND context_policy_version = 'summary-context-v1' AND "
        "working_summary_policy_version = 'task-working-summary-v1' AND "
        "evidence_policy_version = 'task-evidence-v1' AND prompt_version = "
        "'summary-context-v1' AND model_budget IS NOT NULL AND COALESCE(model_budget #>> "
        "'{profile,structured_output_method}', '') = 'function_calling')) OR "
        "(coordination_experience_policy_version = 'coordination-experience-v1' AND "
        "workflow_version = 'v3' AND COALESCE(graph_version, '') = "
        "'dynamic-investigation-graph-v16' AND data_source_mode = 'live' AND "
        "COALESCE(evidence_policy_version, '') = 'task-evidence-live-dynamic-mcp-v1' AND "
        "status IN ('queued', 'running', 'failed', 'await_human', 'await_candidate') AND "
        "COALESCE(coordination_policy_version, '') = 'dynamic-coordination-v1' AND "
        "call_budget IS NOT NULL AND model_budget IS NOT NULL AND "
        "COALESCE(context_policy_version, '') = 'summary-context-v1' AND "
        "COALESCE(working_summary_policy_version, '') = 'task-working-summary-v1' AND "
        "prompt_version = 'summary-context-v1' AND COALESCE(model_budget #>> "
        "'{profile,structured_output_method}', '') = 'function_calling')"
    ),
    "ck_investigation_runs_handoff_scope": (
        "status NOT IN ('await_human', 'await_candidate') OR (workflow_version = 'v3' AND "
        "graph_version = 'dynamic-investigation-graph-v15' AND data_source_mode = 'frozen') "
        "OR (workflow_version = 'v3' AND COALESCE(graph_version, '') = "
        "'dynamic-investigation-graph-v16' AND data_source_mode = 'live' AND "
        "COALESCE(evidence_policy_version, '') = 'task-evidence-live-dynamic-mcp-v1')"
    ),
}

_PREVIOUS_HANDOFF_INSERT = """
CREATE OR REPLACE FUNCTION validate_terminal_handoff_insert() RETURNS trigger AS $$
DECLARE
  parent investigation_attempts%ROWTYPE;
  work work_items%ROWTYPE;
  run investigation_runs%ROWTYPE;
  route investigation_hypothesis_review_routes%ROWTYPE;
  source_round investigation_rounds%ROWTYPE;
BEGIN
  SELECT * INTO work FROM work_items
    WHERE investigation_run_id = NEW.investigation_run_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION '终端交接缺少父任务';
  END IF;
  SELECT * INTO run FROM investigation_runs WHERE id = NEW.investigation_run_id;
  SELECT * INTO parent FROM investigation_attempts
    WHERE id = NEW.investigation_attempt_id
      AND investigation_run_id = NEW.investigation_run_id;
  IF run.id IS NULL OR parent.id IS NULL OR run.status <> 'running'
     OR run.workflow_version <> 'v3'
     OR run.graph_version IS DISTINCT FROM 'dynamic-investigation-graph-v15'
     OR run.data_source_mode <> 'frozen' OR run.mode <> 'multi_agent'
     OR parent.status <> 'running' OR work.status <> 'running'
     OR parent.work_item_id IS DISTINCT FROM work.id
     OR work.lease_token IS DISTINCT FROM parent.lease_token
     OR work.locked_until IS NULL OR work.locked_until <= NEW.created_at
     OR parent.lease_expires_at <= NEW.created_at
     OR parent.started_at > NEW.created_at THEN
    RAISE EXCEPTION '终端交接需要当前有效父租约与运行';
  END IF;
  PERFORM 1 FROM incidents WHERE id = run.incident_id AND status = 'investigating'
    FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION '终端交接需要仍可调查的事故';
  END IF;
  SELECT * INTO route FROM investigation_hypothesis_review_routes
    WHERE id = NEW.source_review_route_id
      AND investigation_run_id = NEW.investigation_run_id;
  IF NOT FOUND OR route.content_sha256 IS DISTINCT FROM NEW.source_sha256
     OR (route.decision ->> 'route') IS DISTINCT FROM NEW.destination
     OR NEW.destination NOT IN ('await_human', 'candidate_evaluation')
     OR route.created_at > NEW.created_at THEN
    RAISE EXCEPTION '终端交接来源路由不匹配';
  END IF;
  SELECT * INTO source_round FROM investigation_rounds
    WHERE id = route.round_id AND investigation_run_id = NEW.investigation_run_id;
  IF NOT FOUND OR source_round.status <> 'completed'
     OR source_round.completed_at IS NULL
     OR source_round.completed_at > route.created_at
     OR EXISTS (SELECT 1 FROM investigation_rounds
                WHERE investigation_run_id = NEW.investigation_run_id
                  AND (round_number > source_round.round_number OR status <> 'completed'))
     OR EXISTS (SELECT 1 FROM agent_tasks
                WHERE investigation_run_id = NEW.investigation_run_id
                  AND status <> 'succeeded')
     OR EXISTS (SELECT 1 FROM agent_task_attempts
                WHERE investigation_run_id = NEW.investigation_run_id AND status = 'running')
     OR EXISTS (SELECT 1 FROM investigation_call_reservations
                WHERE investigation_run_id = NEW.investigation_run_id AND status = 'reserved')
  THEN
    RAISE EXCEPTION '终端交接必须来自最高已完成轮次且无未结算工作';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_LIVE_HANDOFF_INSERT = """
CREATE OR REPLACE FUNCTION validate_terminal_handoff_insert() RETURNS trigger AS $$
DECLARE
  parent investigation_attempts%ROWTYPE;
  work work_items%ROWTYPE;
  run investigation_runs%ROWTYPE;
  route investigation_hypothesis_review_routes%ROWTYPE;
  source_round investigation_rounds%ROWTYPE;
BEGIN
  SELECT * INTO work FROM work_items
    WHERE investigation_run_id = NEW.investigation_run_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION '终端交接缺少父任务';
  END IF;
  SELECT * INTO run FROM investigation_runs WHERE id = NEW.investigation_run_id;
  SELECT * INTO parent FROM investigation_attempts
    WHERE id = NEW.investigation_attempt_id
      AND investigation_run_id = NEW.investigation_run_id;
  IF run.id IS NULL OR parent.id IS NULL OR run.status <> 'running'
     OR run.workflow_version <> 'v3'
     OR NOT ((COALESCE(run.graph_version, '') = 'dynamic-investigation-graph-v15'
       AND run.data_source_mode = 'frozen') OR
       (COALESCE(run.graph_version, '') = 'dynamic-investigation-graph-v16'
        AND run.data_source_mode = 'live'
        AND run.evidence_policy_version = 'task-evidence-live-dynamic-mcp-v1'))
     OR run.mode <> 'multi_agent'
     OR parent.status <> 'running' OR work.status <> 'running'
     OR parent.work_item_id IS DISTINCT FROM work.id
     OR work.lease_token IS DISTINCT FROM parent.lease_token
     OR work.locked_until IS NULL OR work.locked_until <= NEW.created_at
     OR parent.lease_expires_at <= NEW.created_at
     OR parent.started_at > NEW.created_at THEN
    RAISE EXCEPTION '终端交接需要当前有效父租约与运行';
  END IF;
  PERFORM 1 FROM incidents WHERE id = run.incident_id AND status = 'investigating'
    FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION '终端交接需要仍可调查的事故';
  END IF;
  SELECT * INTO route FROM investigation_hypothesis_review_routes
    WHERE id = NEW.source_review_route_id
      AND investigation_run_id = NEW.investigation_run_id;
  IF NOT FOUND OR route.content_sha256 IS DISTINCT FROM NEW.source_sha256
     OR (route.decision ->> 'route') IS DISTINCT FROM NEW.destination
     OR NEW.destination NOT IN ('await_human', 'candidate_evaluation')
     OR route.created_at > NEW.created_at THEN
    RAISE EXCEPTION '终端交接来源路由不匹配';
  END IF;
  SELECT * INTO source_round FROM investigation_rounds
    WHERE id = route.round_id AND investigation_run_id = NEW.investigation_run_id;
  IF NOT FOUND OR source_round.status <> 'completed'
     OR source_round.completed_at IS NULL
     OR source_round.completed_at > route.created_at
     OR EXISTS (SELECT 1 FROM investigation_rounds
                WHERE investigation_run_id = NEW.investigation_run_id
                  AND (round_number > source_round.round_number OR status <> 'completed'))
     OR EXISTS (SELECT 1 FROM agent_tasks
                WHERE investigation_run_id = NEW.investigation_run_id
                  AND status <> 'succeeded')
     OR EXISTS (SELECT 1 FROM agent_task_attempts
                WHERE investigation_run_id = NEW.investigation_run_id AND status = 'running')
     OR EXISTS (SELECT 1 FROM investigation_call_reservations
                WHERE investigation_run_id = NEW.investigation_run_id AND status = 'reserved')
  THEN
    RAISE EXCEPTION '终端交接必须来自最高已完成轮次且无未结算工作';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_LIVE_SOURCE_GUARDS = """
CREATE FUNCTION protect_live_dynamic_source() RETURNS trigger AS $$
BEGIN
 IF OLD.graph_version = 'dynamic-investigation-graph-v16'
    OR NEW.graph_version = 'dynamic-investigation-graph-v16'
    OR OLD.evidence_policy_version = 'task-evidence-live-dynamic-mcp-v1'
    OR NEW.evidence_policy_version = 'task-evidence-live-dynamic-mcp-v1' THEN
   IF ROW(NEW.workflow_version,NEW.graph_version,NEW.data_source_mode,
          NEW.evidence_policy_version,NEW.observation_cutoff_at,NEW.incident_id,
          NEW.graph_thread_id,NEW.mode,NEW.created_at) IS DISTINCT FROM
      ROW(OLD.workflow_version,OLD.graph_version,OLD.data_source_mode,
          OLD.evidence_policy_version,OLD.observation_cutoff_at,OLD.incident_id,
          OLD.graph_thread_id,OLD.mode,OLD.created_at) THEN
     RAISE EXCEPTION '现场动态图来源、范围与恢复版本在创建后不可修改';
   END IF;
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER live_dynamic_source_immutable BEFORE UPDATE ON investigation_runs
 FOR EACH ROW EXECUTE FUNCTION protect_live_dynamic_source();

CREATE FUNCTION assert_live_dynamic_assignment(run investigation_runs,assignment JSONB)
RETURNS VOID AS $$
DECLARE start_at TIMESTAMPTZ; end_at TIMESTAMPTZ; seconds NUMERIC; maximum INTEGER;
 service TEXT; snapshot_id TEXT;
BEGIN
 IF run.evidence_policy_version IS DISTINCT FROM 'task-evidence-live-dynamic-mcp-v1'
 THEN RETURN; END IF;
 SELECT service_name INTO service FROM incidents WHERE id=run.incident_id;
 start_at:=(assignment->>'window_start')::timestamptz;
 end_at:=(assignment->>'window_end')::timestamptz;
 seconds:=extract(epoch FROM end_at-start_at);
 maximum:=CASE assignment->>'role' WHEN 'change' THEN 21600
   WHEN 'log' THEN 900 WHEN 'metric' THEN 900 ELSE 0 END;
 snapshot_id:='live-v1:'||run.id::text||':'||
   to_char(run.observation_cutoff_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS')||'+00:00';
 IF (run.workflow_version='v3' AND run.graph_version='dynamic-investigation-graph-v16'
   AND run.data_source_mode='live' AND assignment->>'service_name'=service
   AND assignment->>'task_key'=assignment->>'role'
   AND isfinite(start_at) AND isfinite(end_at) AND end_at=run.observation_cutoff_at
   AND seconds>0 AND seconds<=maximum AND mod(seconds,60)=0
   AND assignment->>'evidence_snapshot_id'=snapshot_id) IS NOT TRUE THEN
   RAISE EXCEPTION '动态现场任务来源、服务、完整分钟或专业窗口不匹配';
 END IF;
END;
$$ LANGUAGE plpgsql;
CREATE FUNCTION guard_live_dynamic_task() RETURNS trigger AS $$
DECLARE run investigation_runs%ROWTYPE;
BEGIN
 IF TG_OP='UPDATE' THEN
   SELECT * INTO run FROM investigation_runs WHERE id=OLD.investigation_run_id;
   IF run.evidence_policy_version='task-evidence-live-dynamic-mcp-v1'
     AND ROW(NEW.assignment,NEW.investigation_run_id,NEW.round_id,NEW.task_key)
       IS DISTINCT FROM ROW(OLD.assignment,OLD.investigation_run_id,OLD.round_id,OLD.task_key)
   THEN RAISE EXCEPTION '动态现场任务创建后不可改变范围'; END IF;
 END IF;
 SELECT * INTO run FROM investigation_runs WHERE id=NEW.investigation_run_id;
 IF run.evidence_policy_version='task-evidence-live-dynamic-mcp-v1' THEN
   IF NEW.task_key IS DISTINCT FROM NEW.assignment->>'task_key'
   THEN RAISE EXCEPTION '动态现场任务键与持久分派不一致'; END IF;
   IF TG_OP='UPDATE' AND ROW(NEW.assignment,NEW.investigation_run_id,NEW.round_id,NEW.task_key)
     IS DISTINCT FROM ROW(OLD.assignment,OLD.investigation_run_id,OLD.round_id,OLD.task_key) THEN
     RAISE EXCEPTION '动态现场任务创建后不可改变范围';
   END IF;
   PERFORM assert_live_dynamic_assignment(run,NEW.assignment);
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER live_dynamic_task_scope BEFORE INSERT OR UPDATE ON agent_tasks
 FOR EACH ROW EXECUTE FUNCTION guard_live_dynamic_task();

CREATE FUNCTION guard_live_dynamic_evidence() RETURNS trigger AS $$
DECLARE run investigation_runs%ROWTYPE; task agent_tasks%ROWTYPE;
 call investigation_call_reservations%ROWTYPE; item JSONB; snapshot JSONB;
 start_at TIMESTAMPTZ; end_at TIMESTAMPTZ; source_name TEXT; expected_operation TEXT;
BEGIN
 IF TG_OP IN ('UPDATE','DELETE') THEN
   SELECT * INTO run FROM investigation_runs WHERE id=OLD.investigation_run_id;
   IF run.evidence_policy_version='task-evidence-live-dynamic-mcp-v1'
   THEN RAISE EXCEPTION '动态现场证据快照不可改写或删除'; END IF;
   IF TG_OP='DELETE' THEN RETURN OLD; END IF;
   SELECT * INTO run FROM investigation_runs WHERE id=NEW.investigation_run_id;
 ELSE
   SELECT * INTO run FROM investigation_runs WHERE id=NEW.investigation_run_id;
 END IF;
 IF run.evidence_policy_version IS DISTINCT FROM 'task-evidence-live-dynamic-mcp-v1'
 THEN
   IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
 END IF;
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '动态现场证据快照不可改写或删除'; END IF;
 SELECT * INTO task FROM agent_tasks WHERE id=NEW.task_id;
 SELECT * INTO call FROM investigation_call_reservations WHERE id=NEW.call_id;
 PERFORM assert_live_dynamic_assignment(run,task.assignment);
 snapshot:=NEW.snapshot;
 start_at:=(task.assignment->>'window_start')::timestamptz;
 end_at:=(task.assignment->>'window_end')::timestamptz;
 source_name:=CASE task.assignment->>'role' WHEN 'log' THEN 'loki-fault-lab'
   WHEN 'metric' THEN 'prometheus-fault-lab' ELSE 'change-simulator' END;
 expected_operation:=CASE task.assignment->>'role' WHEN 'log' THEN 'query_logs'
   WHEN 'metric' THEN 'query_metrics' ELSE 'list_changes' END;
 IF (task.investigation_run_id=run.id AND call.investigation_run_id=run.id
   AND call.status='succeeded' AND call.kind='tool' AND call.operation=expected_operation
   AND call.task_id=task.id AND call.task_attempt_id=NEW.attempt_id
   AND call.finished_at<=NEW.created_at
   AND snapshot->>'version'=run.evidence_policy_version
   AND snapshot->>'data_source_mode'='live'
   AND snapshot->'evidence_snapshot_id'=task.assignment->'evidence_snapshot_id'
   AND snapshot->'role'=task.assignment->'role'
   AND snapshot->'service_name'=task.assignment->'service_name'
   AND (snapshot->>'window_start')::timestamptz=start_at
   AND (snapshot->>'window_end')::timestamptz=end_at
   AND jsonb_typeof(snapshot->'evidence')='array'
   AND jsonb_array_length(snapshot->'evidence')<=20
   AND octet_length(convert_to(candidate_canonical_json(snapshot),'UTF8'))<=131072
   AND NEW.content_sha256=encode(sha256(convert_to(
       candidate_canonical_json(snapshot),'UTF8')),'hex')
   AND (SELECT count(DISTINCT value->>'evidence_id')
        FROM jsonb_array_elements(snapshot->'evidence'))=jsonb_array_length(snapshot->'evidence'))
 IS NOT TRUE THEN RAISE EXCEPTION '动态现场证据与任务、成功调用或内容散列不一致'; END IF;
 FOR item IN SELECT value FROM jsonb_array_elements(snapshot->'evidence') LOOP
   IF (item->>'kind'=snapshot->>'role' AND item->>'source'=source_name
     AND (item->>'observed_at')::timestamptz BETWEEN start_at AND end_at
     AND item#>'{attributes,service_name}'=snapshot->'service_name'
     AND item#>>'{attributes,window_start}'=replace(snapshot->>'window_start','Z','+00:00')
     AND item#>>'{attributes,window_end}'=replace(snapshot->>'window_end','Z','+00:00'))
   IS NOT TRUE THEN RAISE EXCEPTION '动态现场证据来源、采集时间或精确窗口不符'; END IF;
 END LOOP;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER live_dynamic_evidence_source BEFORE INSERT OR UPDATE OR DELETE ON agent_task_evidence
 FOR EACH ROW EXECUTE FUNCTION guard_live_dynamic_evidence();
"""
