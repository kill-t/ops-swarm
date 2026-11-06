"""固定现场回流输入策略并同步四阶段 PostgreSQL 重建。"""

from alembic import op

revision = "0100_reflow_input_projection"
down_revision = "0099_live_reflow_handoff"
branch_labels = None
depends_on = None

_POLICY = """
ALTER TABLE investigation_runs ADD COLUMN reflow_input_policy VARCHAR(32);
ALTER TABLE investigation_runs ADD CONSTRAINT ck_investigation_runs_reflow_input_policy CHECK (
  reflow_input_policy IS NULL OR (
    reflow_input_policy='reflow-projection-v1' AND
    graph_version IS NOT DISTINCT FROM 'dynamic-investigation-graph-v16'
  )
);
CREATE FUNCTION guard_reflow_input_policy() RETURNS trigger AS $$
BEGIN
  IF NEW.reflow_input_policy IS DISTINCT FROM OLD.reflow_input_policy THEN
    RAISE EXCEPTION '运行创建后不能变更回流输入策略';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER investigation_runs_reflow_input_policy_guard
  BEFORE UPDATE ON investigation_runs FOR EACH ROW EXECUTE FUNCTION guard_reflow_input_policy();
"""

_PROJECTION = """
CREATE FUNCTION reflow_project_source(source JSONB) RETURNS JSONB AS $$
DECLARE original JSONB; candidate JSONB; prior JSONB; history JSONB;
  messages JSONB; materials JSONB; projected JSONB;
BEGIN
  SELECT value->'content' INTO original FROM jsonb_array_elements(source->'materials')
    WHERE value->>'kind'='original_investigation';
  IF NOT (original ? 'input_policy') THEN RETURN source; END IF;
  IF original->>'input_policy' IS DISTINCT FROM 'reflow-projection-v1' THEN RETURN NULL; END IF;
  candidate:=original->'candidate_source';
  SELECT jsonb_agg(value->'content') INTO messages
    FROM jsonb_array_elements(candidate#>'{prior_input,messages}') WHERE value->>'role'='user';
  IF jsonb_array_length(messages) IS DISTINCT FROM 1 THEN RETURN NULL; END IF;
  BEGIN
    prior:=(messages->>0)::jsonb;
  EXCEPTION WHEN invalid_text_representation THEN
    RETURN NULL;
  END;
  IF jsonb_typeof(prior) IS DISTINCT FROM 'object' OR NOT (
    prior ?& ARRAY['incident','task_context','working_summaries','evidence','open_hypothesis']
    AND prior-ARRAY['incident','task_context','working_summaries','evidence','open_hypothesis',
      'output_json_schema']
      ='{}'::jsonb
  ) THEN RETURN NULL; END IF;
  history:=jsonb_build_object(
    'incident',prior->'incident','task_context',prior->'task_context',
    'working_summaries',prior->'working_summaries','evidence',candidate->'evidence',
    'hypothesis',candidate->'hypothesis','failed_calls',original->'failed_calls',
    'input_policy',original->'input_policy');
  IF prior->'evidence' IS DISTINCT FROM candidate->'evidence' THEN
    history:=history||jsonb_build_object('prior_evidence',prior->'evidence');
  END IF;
  IF prior->'open_hypothesis' IS DISTINCT FROM candidate->'hypothesis' THEN
    history:=history||jsonb_build_object('prior_open_hypothesis',prior->'open_hypothesis');
  END IF;
  IF candidate->'human_route' <> 'null'::jsonb THEN
    history:=history||jsonb_build_object(
      'unresolved_constraints',candidate#>'{human_route,decision,unresolved_constraints}');
  END IF;
  SELECT jsonb_agg(CASE value->>'kind'
    WHEN 'original_investigation' THEN jsonb_set(value,'{content}',history)
    WHEN 'execution' THEN jsonb_set(value,'{content}',
      jsonb_build_object('result',value#>'{content,result}'))
    WHEN 'admission' THEN value-'content'
    ELSE value END ORDER BY ordinal) INTO materials
    FROM jsonb_array_elements(source->'materials') WITH ORDINALITY AS item(value,ordinal);

  projected:=jsonb_build_object(
    'version',source->'version','investigation_run_id',source->'investigation_run_id',
    'next_round_number',source->'next_round_number','existing_task_count',source->'existing_task_count',
    'maximum_tasks',source->'maximum_tasks','coordination_policy',source->'coordination_policy',
    'materials',materials);
  IF source ? 'professionals' THEN
    projected:=projected||jsonb_build_object('professionals',(
      SELECT jsonb_agg(jsonb_build_object('task_id',value->'task_id',
        'assignment',value->'assignment','effective_window',value->'effective_window',
        'evidence',value->'evidence','report',value#>'{result,report}') ORDER BY ordinal)
      FROM jsonb_array_elements(source->'professionals') WITH ORDINALITY AS item(value,ordinal)
    ));
  END IF;
  IF source ? 'synthesis_result' THEN
    projected:=projected||jsonb_build_object('synthesis_result',source->'synthesis_result');
  END IF;
  RETURN projected;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;
"""

_REBUILD_EDITS = (
    (
        "reflow_source(uuid,timestamptz)",
        "jsonb_build_object('candidate_source',candidate.source,'failed_calls',failed)",
        """jsonb_build_object('candidate_source',candidate.source,'failed_calls',failed)||
       CASE WHEN run.reflow_input_policy IS NULL THEN '{}'::jsonb
       ELSE jsonb_build_object('input_policy',run.reflow_input_policy) END""",
    ),
    (
        "reflow_model_input(jsonb)",
        "'materials',source->'materials'",
        "'materials',reflow_project_source(source)->'materials'",
    ),
    (
        "reflow_packet_blocker(jsonb)",
        " IF octet_length(convert_to(reflow_model_input",
        """ IF reflow_project_source(source) IS NULL THEN
   RETURN 'reflow_projection_invalid'; END IF;
 IF octet_length(convert_to(reflow_model_input""",
    ),
    (
        "reflow_execution_packet(uuid)",
        "payload:=jsonb_build_object('source',source,",
        "payload:=jsonb_build_object('source',reflow_project_source(source),",
    ),
    (
        "reflow_synthesis_model_input(jsonb)",
        "candidate_canonical_json(source)",
        "candidate_canonical_json(reflow_project_source(source))",
    ),
    (
        "reflow_review_model_input(jsonb)",
        "candidate_canonical_json(source)",
        "candidate_canonical_json(reflow_project_source(source))",
    ),
)


def _replace(signature: str, old: str, new: str) -> None:
    op.execute(f"""
    DO $migration$
    DECLARE definition TEXT := pg_get_functiondef('{signature}'::regprocedure);
    BEGIN
      IF length(definition)-length(replace(definition, $old${old}$old$, ''))
           <> length($old${old}$old$) THEN
        RAISE EXCEPTION '回流输入迁移找不到唯一重建位置';
      END IF;
      EXECUTE replace(definition, $old${old}$old$, $new${new}$new$);
    END $migration$;
    """)


def upgrade() -> None:
    op.execute(_POLICY)
    op.execute(_PROJECTION)
    for signature, old, new in _REBUILD_EDITS:
        _replace(signature, old, new)


def downgrade() -> None:
    op.execute("""
    LOCK TABLE investigation_runs IN SHARE ROW EXCLUSIVE MODE;
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM investigation_runs WHERE reflow_input_policy IS NOT NULL) THEN
        RAISE EXCEPTION '不能降级已有运行固定的回流输入策略';
      END IF;
    END $$;
    """)
    for signature, old, new in reversed(_REBUILD_EDITS):
        _replace(signature, new, old)
    op.execute("""
    DROP FUNCTION reflow_project_source(JSONB);
    DROP TRIGGER investigation_runs_reflow_input_policy_guard ON investigation_runs;
    DROP FUNCTION guard_reflow_input_policy();
    ALTER TABLE investigation_runs DROP COLUMN reflow_input_policy;
    """)
