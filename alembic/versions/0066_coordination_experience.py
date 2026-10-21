"""冻结首轮协调实际使用的历史经验上下文。"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0066_coordination_experience"
down_revision: str | None = "0065_experience_replacement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "investigation_runs",
        sa.Column("coordination_experience_policy_version", sa.String(40), nullable=True),
    )
    op.create_check_constraint(
        "ck_investigation_runs_coordination_experience_policy",
        "investigation_runs",
        "coordination_experience_policy_version IS NULL OR ("
        "coordination_experience_policy_version = 'coordination-experience-v1' AND "
        "workflow_version = 'v3' AND "
        "graph_version = 'dynamic-investigation-graph-v15' AND "
        "data_source_mode = 'frozen' AND "
        "context_policy_version = 'summary-context-v1' AND "
        "working_summary_policy_version = 'task-working-summary-v1' AND "
        "evidence_policy_version = 'task-evidence-v1' AND "
        "prompt_version = 'summary-context-v1' AND model_budget IS NOT NULL AND "
        "COALESCE(model_budget #>> '{profile,structured_output_method}', '') = "
        "'function_calling')",
    )
    op.execute(
        """
        CREATE FUNCTION protect_coordination_experience_run_policy() RETURNS trigger AS $$
        BEGIN
          IF NEW.coordination_experience_policy_version IS DISTINCT FROM
             OLD.coordination_experience_policy_version THEN
            RAISE EXCEPTION '协调经验上下文策略在运行创建后不可修改';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER investigation_runs_coordination_experience_policy_immutable
          BEFORE UPDATE OF coordination_experience_policy_version ON investigation_runs
          FOR EACH ROW EXECUTE FUNCTION protect_coordination_experience_run_policy();
        """
    )

    op.create_table(
        "investigation_experience_contexts",
        sa.Column("investigation_run_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_version", sa.String(40), nullable=False),
        sa.Column("first_call_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("id", name="uq_investigation_experience_contexts_id"),
        sa.UniqueConstraint(
            "first_call_id", name="uq_investigation_experience_contexts_first_call"
        ),
        sa.CheckConstraint(
            "policy_version = 'coordination-experience-v1'",
            name="ck_investigation_experience_contexts_policy",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object' AND "
            "octet_length(content::text) <= 196608 AND "
            "COALESCE(content ->> 'id', '') = id::text AND "
            "COALESCE(content ->> 'policy_version', '') = policy_version AND "
            "COALESCE(content ->> 'search_policy_version', '') = 'experience-search-v2' AND "
            "COALESCE(jsonb_typeof(content -> 'scope'), '') = 'object' AND "
            "COALESCE(content #>> '{scope,run_id}', '') = investigation_run_id::text AND "
            "COALESCE(jsonb_typeof(content -> 'selected_at'), '') = 'string' AND "
            "COALESCE(jsonb_typeof(content -> 'items'), '') = 'array' AND "
            "jsonb_array_length(content -> 'items') <= 3",
            name="ck_investigation_experience_contexts_content",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_investigation_experience_contexts_hash",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_run_id"],
            ["investigation_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["first_call_id", "investigation_run_id"],
            [
                "investigation_call_reservations.id",
                "investigation_call_reservations.investigation_run_id",
            ],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_investigation_experience_contexts_first_call",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION guard_coordination_experience_context_insert() RETURNS trigger AS $$
        DECLARE
          run_policy text;
          run_created_at timestamptz;
          selected_at timestamptz;
        BEGIN
          SELECT coordination_experience_policy_version, created_at
            INTO run_policy, run_created_at
            FROM investigation_runs WHERE id = NEW.investigation_run_id;
          IF FOUND AND run_policy IS DISTINCT FROM 'coordination-experience-v1' THEN
            RAISE EXCEPTION '运行未启用协调经验上下文策略';
          END IF;
          BEGIN
            selected_at := (NEW.content ->> 'selected_at')::timestamptz;
          EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION '经验上下文选择时间不合法';
          END;
          IF selected_at IS NULL OR (run_created_at IS NOT NULL AND
             (selected_at < run_created_at OR selected_at > NEW.created_at)) THEN
            RAISE EXCEPTION '经验上下文选择时间超出运行和预留边界';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE FUNCTION protect_coordination_experience_context() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION '协调经验上下文快照不可修改或删除';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER investigation_experience_context_insert_guard
          BEFORE INSERT ON investigation_experience_contexts
          FOR EACH ROW EXECUTE FUNCTION guard_coordination_experience_context_insert();
        CREATE TRIGGER investigation_experience_context_immutable
          BEFORE UPDATE OR DELETE ON investigation_experience_contexts
          FOR EACH ROW EXECUTE FUNCTION protect_coordination_experience_context();
        """
    )

    op.execute(
        """
        ALTER TABLE investigation_call_reservations
          DROP CONSTRAINT ck_call_reservations_loop_payload;
        ALTER TABLE investigation_call_reservations
          ADD CONSTRAINT ck_call_reservations_loop_payload CHECK
          (((operation IN ('read_log_evidence','read_metric_evidence',
                           'read_change_evidence','read_log_runbook',
                           'read_metric_runbook','read_change_runbook'))
            = (tool_request IS NOT NULL)) AND
           (tool_request IS NULL OR (jsonb_typeof(tool_request) = 'object' AND
            octet_length(tool_request::text) <= 8192)) AND
           (call_result IS NULL OR (status <> 'reserved' AND
            jsonb_typeof(call_result) = 'object' AND
            octet_length(call_result::text) <= 65536 AND
            COALESCE(call_result ->> 'kind', '') = kind AND
            ((kind = 'model' AND
              ((operation = 'coordinate' AND
                COALESCE(model_input ->> 'version', '') IN
                  ('coordination-model-input-v1','coordination-experience-input-v1') AND
                COALESCE(call_result ->> 'version', '') =
                  'coordination-model-result-v1') OR
               (operation = 'synthesize' AND NOT (model_input ? 'version') AND
                COALESCE(call_result ->> 'version', '') =
                  'open-hypothesis-synthesis-result-v1') OR
               (operation = 'review' AND NOT (model_input ? 'version') AND
                COALESCE(call_result ->> 'version', '') =
                  'hypothesis-review-result-v1') OR
               (operation NOT IN ('coordinate', 'synthesize', 'review') AND
                COALESCE(model_input ->> 'version', '') IN
                  ('evidence-loop-input-v1','runbook-loop-input-v1')))) OR
             (kind = 'tool' AND tool_request IS NOT NULL)))));

        CREATE FUNCTION guard_coordination_experience_call_insert() RETURNS trigger AS $$
        DECLARE
          run_policy text;
          frozen investigation_experience_contexts%ROWTYPE;
          historical_experience jsonb;
        BEGIN
          IF NEW.operation <> 'coordinate' THEN
            RETURN NEW;
          END IF;
          SELECT coordination_experience_policy_version INTO run_policy
            FROM investigation_runs WHERE id = NEW.investigation_run_id;
          IF run_policy IS NULL THEN
            IF COALESCE(NEW.model_input ->> 'version', '') <>
               'coordination-model-input-v1' THEN
              RAISE EXCEPTION '未启用经验策略的协调调用必须使用旧输入契约';
            END IF;
            RETURN NEW;
          END IF;
          IF run_policy <> 'coordination-experience-v1' OR
             COALESCE(NEW.model_input ->> 'version', '') <>
               'coordination-experience-input-v1' THEN
            RAISE EXCEPTION '经验策略运行的协调调用契约不匹配';
          END IF;
          SELECT * INTO frozen FROM investigation_experience_contexts
            WHERE investigation_run_id = NEW.investigation_run_id;
          IF NOT FOUND THEN
            RAISE EXCEPTION '经验策略协调调用缺少同运行冻结快照';
          END IF;
          BEGIN
            historical_experience :=
              ((NEW.model_input #>> '{messages,1,content}')::jsonb)
              -> 'historical_experience';
          EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION '协调调用中的历史经验输入不是合法 JSON';
          END;
          IF historical_experience IS DISTINCT FROM frozen.content OR
             COALESCE(NEW.context_manifest ->> 'version', '') <>
               'coordination-experience-v1' OR
             COALESCE(NEW.context_manifest #>> '{materials,2,kind}', '') <>
               'experience_selection' OR
             COALESCE(NEW.context_manifest #>> '{materials,2,material_id}', '') <>
               frozen.id::text OR
             COALESCE(NEW.context_manifest #>> '{materials,2,version}', '') <>
               frozen.policy_version OR
             COALESCE(NEW.context_manifest #>> '{materials,2,content_sha256}', '') <>
               frozen.content_hash THEN
            RAISE EXCEPTION '协调调用未精确绑定冻结经验选择';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER investigation_call_coordination_experience_guard
          BEFORE INSERT ON investigation_call_reservations
          FOR EACH ROW EXECUTE FUNCTION guard_coordination_experience_call_insert();

        CREATE FUNCTION validate_coordination_experience_first_call() RETURNS trigger AS $$
        DECLARE
          first_call investigation_call_reservations%ROWTYPE;
        BEGIN
          SELECT * INTO first_call FROM investigation_call_reservations
            WHERE id = NEW.first_call_id
              AND investigation_run_id = NEW.investigation_run_id;
          IF NOT FOUND OR first_call.operation <> 'coordinate' OR
             first_call.kind <> 'model' OR first_call.task_id IS NOT NULL OR
             first_call.task_attempt_id IS NOT NULL OR first_call.invocation_index <> 0 OR
             first_call.reserved_at IS DISTINCT FROM NEW.created_at THEN
            RAISE EXCEPTION '经验快照首次来源不是同运行同时间的根级协调预留';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE CONSTRAINT TRIGGER investigation_experience_context_first_call_guard
          AFTER INSERT ON investigation_experience_contexts
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION validate_coordination_experience_first_call();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        LOCK TABLE investigation_experience_contexts,
                   investigation_call_reservations,
                   investigation_runs IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM investigation_runs
              WHERE coordination_experience_policy_version IS NOT NULL
          ) OR EXISTS (
            SELECT 1 FROM investigation_experience_contexts
          ) OR EXISTS (
            SELECT 1 FROM investigation_call_reservations
              WHERE model_input ->> 'version' = 'coordination-experience-input-v1'
                 OR context_manifest ->> 'version' = 'coordination-experience-v1'
                 OR context_manifest ->> 'prompt_version' =
                      'coordination-experience-plan-v1'
          ) THEN
            RAISE EXCEPTION '不能删除已保存的协调经验策略、快照或调用审计';
          END IF;
        END $$;

        DROP TRIGGER investigation_call_coordination_experience_guard
          ON investigation_call_reservations;
        DROP FUNCTION guard_coordination_experience_call_insert();
        DROP TRIGGER investigation_experience_context_first_call_guard
          ON investigation_experience_contexts;
        DROP FUNCTION validate_coordination_experience_first_call();

        ALTER TABLE investigation_call_reservations
          DROP CONSTRAINT ck_call_reservations_loop_payload;
        ALTER TABLE investigation_call_reservations
          ADD CONSTRAINT ck_call_reservations_loop_payload CHECK
          (((operation IN ('read_log_evidence','read_metric_evidence',
                           'read_change_evidence','read_log_runbook',
                           'read_metric_runbook','read_change_runbook'))
            = (tool_request IS NOT NULL)) AND
           (tool_request IS NULL OR (jsonb_typeof(tool_request) = 'object' AND
            octet_length(tool_request::text) <= 8192)) AND
           (call_result IS NULL OR (status <> 'reserved' AND
            jsonb_typeof(call_result) = 'object' AND
            octet_length(call_result::text) <= 65536 AND
            COALESCE(call_result ->> 'kind', '') = kind AND
            ((kind = 'model' AND
              ((operation = 'coordinate' AND
                COALESCE(model_input ->> 'version', '') =
                  'coordination-model-input-v1' AND
                COALESCE(call_result ->> 'version', '') =
                  'coordination-model-result-v1') OR
               (operation = 'synthesize' AND NOT (model_input ? 'version') AND
                COALESCE(call_result ->> 'version', '') =
                  'open-hypothesis-synthesis-result-v1') OR
               (operation = 'review' AND NOT (model_input ? 'version') AND
                COALESCE(call_result ->> 'version', '') =
                  'hypothesis-review-result-v1') OR
               (operation NOT IN ('coordinate', 'synthesize', 'review') AND
                COALESCE(model_input ->> 'version', '') IN
                  ('evidence-loop-input-v1','runbook-loop-input-v1')))) OR
             (kind = 'tool' AND tool_request IS NOT NULL)))));
        """
    )
    op.execute(
        """
        DROP TRIGGER investigation_experience_context_immutable
          ON investigation_experience_contexts;
        DROP TRIGGER investigation_experience_context_insert_guard
          ON investigation_experience_contexts;
        """
    )
    op.drop_table("investigation_experience_contexts")
    op.execute("DROP FUNCTION protect_coordination_experience_context()")
    op.execute("DROP FUNCTION guard_coordination_experience_context_insert()")
    op.execute(
        """
        DROP TRIGGER investigation_runs_coordination_experience_policy_immutable
          ON investigation_runs;
        DROP FUNCTION protect_coordination_experience_run_policy();
        """
    )
    op.drop_constraint(
        "ck_investigation_runs_coordination_experience_policy",
        "investigation_runs",
        type_="check",
    )
    op.drop_column("investigation_runs", "coordination_experience_policy_version")
