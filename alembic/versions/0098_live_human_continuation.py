"""将现场终端交接接入现有可信人工与候选准入。"""

from alembic import op

revision = "0098_live_human_continuation"
down_revision = "0097_live_dynamic_investigation"
branch_labels = None
depends_on = None

_FROZEN = (
    "COALESCE(graph_version, '') = 'dynamic-investigation-graph-v15' "
    "AND data_source_mode = 'frozen'"
)
_LIVE = (
    "COALESCE(graph_version, '') = 'dynamic-investigation-graph-v16' AND "
    "data_source_mode = 'live' AND "
    "COALESCE(evidence_policy_version, '') = 'task-evidence-live-dynamic-mcp-v1'"
)
_SOURCE = (
    "((COALESCE(run.graph_version, '') = 'dynamic-investigation-graph-v15' "
    "AND run.data_source_mode = 'frozen') OR "
    "(COALESCE(run.graph_version, '') = 'dynamic-investigation-graph-v16' "
    "AND run.data_source_mode = 'live' "
    "AND COALESCE(run.evidence_policy_version, '') = 'task-evidence-live-dynamic-mcp-v1'))"
)
_FROZEN_GUARDS = (
    (
        "validate_human_wait_event_insert()",
        "run.graph_version IS DISTINCT FROM 'dynamic-investigation-graph-v15'\n"
        "    OR run.data_source_mode <> 'frozen'",
        f"NOT {_SOURCE}",
    ),
    *(
        (
            signature,
            "run.workflow_version<>'v3' OR run.data_source_mode<>'frozen'\n"
            "    OR run.graph_version IS DISTINCT FROM 'dynamic-investigation-graph-v15'",
            f"run.workflow_version<>'v3' OR NOT {_SOURCE}",
        )
        for signature in ("guard_human_preparation_job()", "guard_human_review_job()")
    ),
    (
        "assert_candidate_source(investigation_candidate_evaluation_jobs,boolean)",
        "run.workflow_version='v3' AND run.graph_version='dynamic-investigation-graph-v15'\n"
        "    AND run.data_source_mode='frozen'",
        f"run.workflow_version='v3' AND {_SOURCE}",
    ),
    (
        "guard_candidate_settlement()",
        "run.workflow_version='v3' AND run.graph_version='dynamic-investigation-graph-v15'\n"
        "   AND run.data_source_mode='frozen'",
        f"run.workflow_version='v3' AND {_SOURCE}",
    ),
    (
        "guard_human_preparation_artifact()",
        "OR NEW.content->>'source_sha256' IS DISTINCT FROM job.source_sha256 THEN",
        "OR NEW.content->>'source_sha256' IS DISTINCT FROM job.source_sha256\n"
        "    OR NEW.content #>> '{prepared_input,manifest,graph_version}' "
        "IS DISTINCT FROM run.graph_version THEN",
    ),
)


_PREVIOUS_POLICIES = {
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
}


def _policy_expression(name: str, *, live: bool) -> str:
    expression = _PREVIOUS_POLICIES[name]
    if live:
        expression = expression.replace(
            "'await_candidate') AND", "'await_candidate', 'cancelled', 'settled') AND"
        )
    return expression


def _replace_guard(signature: str, old: str, new: str) -> None:
    # 只替换一个已知条件，原函数的事务锁、来源、租约及审计检查随定义保留。
    op.execute(f"""
    DO $migration$
    DECLARE definition TEXT := pg_get_functiondef('{signature}'::regprocedure);
    BEGIN
      IF length(definition)-length(replace(definition, $old${old}$old$, ''))
           <> length($old${old}$old$) THEN
        RAISE EXCEPTION '现场续接迁移找不到唯一原生条件';
      END IF;
      EXECUTE replace(definition, $old${old}$old$, $new${new}$new$);
    END $migration$;
    """)


def _replace_scope(name: str, expression: str) -> None:
    op.execute(f"ALTER TABLE investigation_runs DROP CONSTRAINT {name}")
    op.execute(f"ALTER TABLE investigation_runs ADD CONSTRAINT {name} CHECK ({expression})")


def _replace_scopes(*, live: bool) -> None:
    scope = f"(({_FROZEN}) OR ({_LIVE}))" if live else f"({_FROZEN})"
    _replace_scope(
        "ck_investigation_runs_cancelled_scope",
        f"status <> 'cancelled' OR (workflow_version = 'v3' AND {scope})",
    )
    _replace_scope(
        "ck_investigation_runs_settled_scope",
        f"status <> 'settled' OR (workflow_version = 'v3' AND {scope} "
        "AND result IS NULL AND metrics IS NULL AND last_error IS NULL)",
    )
    for name in (
        "ck_investigation_runs_dynamic_policy",
        "ck_investigation_runs_coordination_experience_policy",
    ):
        _replace_scope(name, _policy_expression(name, live=live))


def upgrade() -> None:
    _replace_scopes(live=True)
    for signature, previous, current in _FROZEN_GUARDS:
        _replace_guard(signature, previous, current)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (
        SELECT 1 FROM investigation_runs r
        WHERE r.graph_version = 'dynamic-investigation-graph-v16'
          AND (r.status IN ('cancelled', 'settled')
            OR EXISTS (SELECT 1 FROM investigation_human_wait_events e
                       WHERE e.investigation_run_id = r.id)
            OR EXISTS (SELECT 1 FROM investigation_candidate_evaluation_jobs c
                       WHERE c.investigation_run_id = r.id))
      ) THEN RAISE EXCEPTION '不能降级已消费的现场人工或候选来源'; END IF;
    END $$;
    """)
    for signature, previous, current in _FROZEN_GUARDS:
        _replace_guard(signature, current, previous)
    _replace_scopes(live=False)
