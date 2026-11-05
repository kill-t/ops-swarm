"""把现场一次回流收束为持久人工交接，保留冻结图原路由。"""

from alembic import op

revision = "0099_live_reflow_handoff"
down_revision = "0098_live_human_continuation"
branch_labels = None
depends_on = None

_OLD = "IF reason IS NOT NULL THEN blockers:=blockers||jsonb_build_array(reason); END IF;"
_NEW = (
    _OLD
    + """
 IF EXISTS (SELECT 1 FROM investigation_runs r WHERE r.id=run_id
            AND r.graph_version='dynamic-investigation-graph-v16') THEN
   blockers:=blockers||'"live_reflow_manual_takeover"'::jsonb;
 END IF;"""
)


def _replace_compiler(old: str, new: str) -> None:
    # 只修改路由结论，继续由原事务重建证据、审核、预算和历史依据。
    op.execute(f"""
    DO $migration$
    DECLARE definition TEXT := pg_get_functiondef(
      'reflow_routing_content(uuid,timestamptz,bigint)'::regprocedure
    );
    BEGIN
      IF length(definition)-length(replace(definition, $old${old}$old$, ''))
           <> length($old${old}$old$) THEN
        RAISE EXCEPTION '现场回流迁移找不到唯一原生路由条件';
      END IF;
      EXECUTE replace(definition, $old${old}$old$, $new${new}$new$);
    END $migration$;
    """)


def upgrade() -> None:
    op.execute("LOCK TABLE investigation_reflow_routes IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (
        SELECT 1 FROM investigation_reflow_routes route
        JOIN investigation_runs r ON r.id=route.investigation_run_id
        WHERE r.graph_version='dynamic-investigation-graph-v16'
      ) THEN RAISE EXCEPTION '已有现场回流路由，不能改变原历史结论'; END IF;
    END $$;
    """)
    _replace_compiler(_OLD, _NEW)


def downgrade() -> None:
    op.execute("LOCK TABLE investigation_reflow_routes IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (
        SELECT 1 FROM investigation_reflow_routes route
        WHERE (route.content->'blocking_reason_codes') ? 'live_reflow_manual_takeover'
      ) THEN RAISE EXCEPTION '不能降级已记录的现场回流人工交接'; END IF;
    END $$;
    """)
    _replace_compiler(_NEW, _OLD)
