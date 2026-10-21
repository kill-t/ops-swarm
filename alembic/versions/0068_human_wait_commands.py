"""持久保存可信人工补充与取消，保留终端交接历史。"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0068_human_wait_commands"
down_revision = "0067_terminal_handoff"
branch_labels = None
depends_on = None


def _lifecycle(*, enabled: bool) -> None:
    cancelled = ", 'cancelled'" if enabled else ""
    events = ", 'run.human_input_saved', 'run.cancelled'" if enabled else ""
    op.execute(f"""
      ALTER TABLE investigation_runs DROP CONSTRAINT ck_investigation_runs_status,
        DROP CONSTRAINT ck_investigation_runs_completion_shape;
      ALTER TABLE investigation_runs ADD CONSTRAINT ck_investigation_runs_status
        CHECK (status IN ('queued','running','succeeded','failed','await_human',
                         'await_candidate'{cancelled})),
        ADD CONSTRAINT ck_investigation_runs_completion_shape CHECK
          ((status IN ('queued','running','await_human','await_candidate')
             AND completed_at IS NULL) OR
           (status IN ('succeeded','failed'{cancelled}) AND completed_at IS NOT NULL));
      ALTER TABLE agent_task_events DROP CONSTRAINT ck_task_events_type;
      ALTER TABLE agent_task_events ADD CONSTRAINT ck_task_events_type CHECK
        (event_type IN ('round.created','round.completed','round.failed','task.started',
          'task.succeeded','task.failed','task.interrupted','task.evidence_saved',
          'run.handed_off'{events}));
    """)


def upgrade() -> None:
    _lifecycle(enabled=True)
    op.execute("""
      ALTER TABLE investigation_runs ADD CONSTRAINT ck_investigation_runs_cancelled_scope
        CHECK (status <> 'cancelled' OR (workflow_version='v3'
          AND graph_version='dynamic-investigation-graph-v15' AND data_source_mode='frozen'));
      ALTER TABLE investigation_terminal_handoffs ADD CONSTRAINT uq_terminal_handoffs_id_run
        UNIQUE (id, investigation_run_id);
    """)
    op.create_table(
        "investigation_human_wait_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("investigation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("handoff_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("subject_id", sa.String(100), nullable=False),
        sa.Column("authorization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("command_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "investigation_run_id", "state_version", name="uq_human_wait_events_version"
        ),
        sa.UniqueConstraint(
            "investigation_run_id", "subject_id", "idempotency_key", name="uq_human_wait_events_key"
        ),
        sa.UniqueConstraint("authorization_id", name="uq_human_wait_events_authorization"),
        sa.ForeignKeyConstraint(
            ["handoff_id", "investigation_run_id"],
            [
                "investigation_terminal_handoffs.id",
                "investigation_terminal_handoffs.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_human_wait_events_handoff",
        ),
        sa.CheckConstraint("state_version BETWEEN 2 AND 17", name="ck_human_wait_events_version"),
        sa.CheckConstraint(
            "action IN ('supplement', 'cancel')", name="ck_human_wait_events_action"
        ),
        sa.CheckConstraint("command_sha256 ~ '^[0-9a-f]{64}$'", name="ck_human_wait_events_hash"),
        sa.CheckConstraint(
            "char_length(btrim(subject_id)) BETWEEN 1 AND 100", name="ck_human_wait_events_subject"
        ),
        sa.CheckConstraint(
            "char_length(idempotency_key) BETWEEN 8 AND 100 AND "
            "idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'",
            name="ck_human_wait_events_key",
        ),
    )
    op.create_table(
        "investigation_human_wait_commands",
        sa.Column("cursor", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("authorization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("investigation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_id", sa.String(100), nullable=False),
        sa.Column("command", postgresql.JSONB(), nullable=False),
        sa.Column("raw_body", sa.Text(), nullable=False),
        sa.Column("command_sha256", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True)),
        sa.Column("error_code", sa.String(80)),
        sa.Column("error_message", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("authorization_id", name="uq_human_wait_commands_authorization"),
        sa.ForeignKeyConstraint(
            ["authorization_id"], ["operator_authorizations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["investigation_human_wait_events.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(command) = 'object' AND octet_length(command::text) <= 32768",
            name="ck_human_wait_commands_body",
        ),
        sa.CheckConstraint(
            "octet_length(raw_body) BETWEEN 2 AND 65536", name="ck_human_wait_commands_raw"
        ),
        sa.CheckConstraint("command_sha256 ~ '^[0-9a-f]{64}$'", name="ck_human_wait_commands_hash"),
        sa.CheckConstraint(
            "char_length(btrim(subject_id)) BETWEEN 1 AND 100",
            name="ck_human_wait_commands_subject",
        ),
        sa.CheckConstraint(
            "(outcome IN ('applied', 'replayed') AND event_id IS NOT NULL "
            "AND error_code IS NULL AND error_message IS NULL AND "
            "((outcome = 'applied' AND http_status=201) OR "
            "(outcome = 'replayed' AND http_status=200))) OR "
            "(outcome='error' AND event_id IS NULL AND error_code IS NOT NULL "
            "AND error_message IS NOT NULL AND http_status IN (404,409))",
            name="ck_human_wait_commands_outcome",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,80}$'",
            name="ck_human_wait_commands_error",
        ),
    )
    op.create_index(
        "ix_human_wait_commands_run_cursor",
        "investigation_human_wait_commands",
        ["investigation_run_id", "cursor"],
    )
    op.create_foreign_key(
        "fk_human_wait_events_command",
        "investigation_human_wait_events",
        "investigation_human_wait_commands",
        ["authorization_id"],
        ["authorization_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.execute(_GUARDS)
    op.execute(_HANDOFF_STATE)


_GUARDS = r"""
CREATE FUNCTION reject_human_wait_mutation() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION '人工等待事实及命令审计不可更新或删除'; END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER human_wait_events_immutable BEFORE UPDATE OR DELETE
  ON investigation_human_wait_events FOR EACH ROW EXECUTE FUNCTION reject_human_wait_mutation();
CREATE TRIGGER human_wait_commands_immutable BEFORE UPDATE OR DELETE
  ON investigation_human_wait_commands FOR EACH ROW EXECUTE FUNCTION reject_human_wait_mutation();

CREATE FUNCTION validate_human_wait_event_insert() RETURNS trigger AS $$
DECLARE
  run investigation_runs%ROWTYPE;
  handoff investigation_terminal_handoffs%ROWTYPE;
  admission operator_authorizations%ROWTYPE;
  previous investigation_human_wait_events%ROWTYPE;
  expected_operation TEXT;
  expected_route TEXT;
BEGIN
  PERFORM 1 FROM work_items WHERE investigation_run_id=NEW.investigation_run_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION '人工等待事件缺少父任务'; END IF;
  SELECT * INTO run FROM investigation_runs WHERE id=NEW.investigation_run_id;
  SELECT * INTO handoff FROM investigation_terminal_handoffs
    WHERE id=NEW.handoff_id AND investigation_run_id=NEW.investigation_run_id;
  SELECT * INTO previous FROM investigation_human_wait_events
    WHERE investigation_run_id=NEW.investigation_run_id ORDER BY state_version DESC LIMIT 1;
  IF run.status IS DISTINCT FROM 'await_human' OR run.workflow_version <> 'v3'
    OR run.graph_version IS DISTINCT FROM 'dynamic-investigation-graph-v15'
    OR run.data_source_mode <> 'frozen' OR handoff.id IS NULL
    OR handoff.destination <> 'await_human' OR NEW.created_at < handoff.created_at
    OR NEW.state_version <> COALESCE(previous.state_version,1)+1
    OR previous.action='cancel' OR NEW.created_at < previous.created_at
    OR (NEW.action='supplement' AND NEW.state_version>16) THEN
    RAISE EXCEPTION '人工等待事件需要精确有效等待来源及连续版本';
  END IF;
  expected_operation := CASE NEW.action WHEN 'supplement' THEN
    'supplement_investigation_wait' ELSE 'cancel_investigation_wait' END;
  expected_route := '/api/v1/investigations/{run_id}/human-wait/' ||
    CASE NEW.action WHEN 'supplement' THEN 'supplements' ELSE 'cancel' END;
  SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
  IF admission.id IS NULL OR admission.policy_version<>'operator-auth-v1'
    OR admission.role<>'engineer' OR NOT admission.permissions ? 'manage_incident'
    OR admission.subject_id IS DISTINCT FROM NEW.subject_id OR admission.method<>'POST'
    OR admission.operation IS DISTINCT FROM expected_operation
    OR admission.route IS DISTINCT FROM expected_route
    OR admission.resources IS DISTINCT FROM
       jsonb_build_object('run_id',NEW.investigation_run_id::text)
    OR admission.authorized_at>NEW.created_at THEN
    RAISE EXCEPTION '人工等待事件缺少同源可信工程师授权';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER human_wait_events_source BEFORE INSERT ON investigation_human_wait_events
  FOR EACH ROW EXECUTE FUNCTION validate_human_wait_event_insert();

CREATE FUNCTION validate_human_wait_command_insert() RETURNS trigger AS $$
DECLARE
  admission operator_authorizations%ROWTYPE;
  event investigation_human_wait_events%ROWTYPE;
  action TEXT;
  expected_operation TEXT;
  expected_route TEXT;
BEGIN
  action := NEW.command->>'action';
  expected_operation := CASE action WHEN 'supplement' THEN
    'supplement_investigation_wait' ELSE 'cancel_investigation_wait' END;
  expected_route := '/api/v1/investigations/{run_id}/human-wait/' ||
    CASE action WHEN 'supplement' THEN 'supplements' ELSE 'cancel' END;
  SELECT * INTO admission FROM operator_authorizations WHERE id=NEW.authorization_id;
  IF admission.id IS NULL OR admission.policy_version<>'operator-auth-v1'
    OR admission.role<>'engineer' OR NOT admission.permissions ? 'manage_incident'
    OR admission.subject_id IS DISTINCT FROM NEW.subject_id OR admission.method<>'POST'
    OR admission.operation IS DISTINCT FROM expected_operation
    OR admission.route IS DISTINCT FROM expected_route
    OR admission.resources IS DISTINCT FROM
       jsonb_build_object('run_id',NEW.investigation_run_id::text)
    OR admission.authorized_at>NEW.created_at
    OR admission.body_sha256 IS DISTINCT FROM
       encode(sha256(convert_to(NEW.raw_body,'UTF8')),'hex') THEN
    RAISE EXCEPTION '人工等待命令缺少精确正文和同源可信授权';
  END IF;
  IF action IS NULL OR action NOT IN ('supplement','cancel')
    OR NEW.command->>'handoff_id' IS NULL
    OR COALESCE(NEW.command->>'source_sha256','') !~ '^[0-9a-f]{64}$'
    OR jsonb_typeof(NEW.command->'expected_wait_version') IS DISTINCT FROM 'number'
    OR NEW.command->'expected_wait_version' NOT IN
       (SELECT to_jsonb(value) FROM generate_series(1,17) value)
    OR COALESCE(char_length(NEW.command->>'idempotency_key'),0) NOT BETWEEN 8 AND 100
    OR (NEW.command->>'idempotency_key') !~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'
    OR COALESCE(char_length(btrim(NEW.command->>'reason')),0) NOT BETWEEN 1 AND 1000 THEN
    RAISE EXCEPTION '人工等待规范命令缺少有效绑定字段';
  END IF;
  IF action='supplement' THEN
    IF jsonb_typeof(NEW.command->'items') IS DISTINCT FROM 'array' THEN
      RAISE EXCEPTION '人工补充材料必须是有界数组';
    END IF;
    IF jsonb_array_length(NEW.command->'items') NOT BETWEEN 1 AND 5 OR EXISTS
      (SELECT 1 FROM jsonb_array_elements(NEW.command->'items') item
       WHERE jsonb_typeof(item) IS DISTINCT FROM 'object'
         OR COALESCE(item->>'kind','') NOT IN ('statement','constraint','question')
         OR COALESCE(char_length(btrim(item->>'text')),0) NOT BETWEEN 1 AND 1000) THEN
      RAISE EXCEPTION '人工补充材料数量或形状无效';
    END IF;
  ELSIF NEW.command ? 'items' THEN
    RAISE EXCEPTION '取消命令不能包含补充材料';
  END IF;
  IF NEW.outcome IN ('applied','replayed') THEN
    SELECT * INTO event FROM investigation_human_wait_events WHERE id=NEW.event_id;
    IF event.id IS NULL OR event.investigation_run_id IS DISTINCT FROM NEW.investigation_run_id
      OR event.subject_id IS DISTINCT FROM NEW.subject_id
      OR event.command_sha256 IS DISTINCT FROM NEW.command_sha256
      OR event.idempotency_key IS DISTINCT FROM NEW.command->>'idempotency_key'
      OR event.action IS DISTINCT FROM action OR event.created_at>NEW.created_at
      OR NEW.command->>'handoff_id' IS DISTINCT FROM event.handoff_id::text
      OR NEW.command->'expected_wait_version' IS DISTINCT FROM to_jsonb(event.state_version-1)
      OR NEW.command->>'source_sha256' IS DISTINCT FROM
         (SELECT source_sha256 FROM investigation_terminal_handoffs WHERE id=event.handoff_id)
      OR (NEW.outcome='applied' AND (event.authorization_id IS DISTINCT FROM NEW.authorization_id
                                    OR event.created_at IS DISTINCT FROM NEW.created_at))
      OR (NEW.outcome='replayed' AND event.authorization_id=NEW.authorization_id) THEN
      RAISE EXCEPTION '人工等待命令结果与原始成功事件不一致';
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER human_wait_commands_source BEFORE INSERT ON investigation_human_wait_commands
  FOR EACH ROW EXECUTE FUNCTION validate_human_wait_command_insert();

CREATE FUNCTION validate_human_wait_event_state() RETURNS trigger AS $$
DECLARE
  event investigation_human_wait_events%ROWTYPE;
  audit investigation_human_wait_commands%ROWTYPE;
  handoff investigation_terminal_handoffs%ROWTYPE;
  run investigation_runs%ROWTYPE;
BEGIN
  SELECT * INTO event FROM investigation_human_wait_events WHERE id=NEW.id;
  SELECT * INTO audit FROM investigation_human_wait_commands
    WHERE authorization_id=event.authorization_id;
  SELECT * INTO handoff FROM investigation_terminal_handoffs WHERE id=event.handoff_id;
  SELECT * INTO run FROM investigation_runs WHERE id=event.investigation_run_id;
  IF audit.authorization_id IS NULL OR audit.outcome<>'applied'
    OR audit.event_id IS DISTINCT FROM event.id
    OR audit.subject_id IS DISTINCT FROM event.subject_id
    OR audit.investigation_run_id IS DISTINCT FROM event.investigation_run_id
    OR audit.command_sha256 IS DISTINCT FROM event.command_sha256
    OR audit.command->>'handoff_id' IS DISTINCT FROM event.handoff_id::text
    OR audit.command->>'source_sha256' IS DISTINCT FROM handoff.source_sha256
    OR audit.command->'expected_wait_version' IS DISTINCT FROM to_jsonb(event.state_version-1)
    OR audit.command->>'action' IS DISTINCT FROM event.action
    OR audit.command->>'idempotency_key' IS DISTINCT FROM event.idempotency_key
    OR audit.created_at IS DISTINCT FROM event.created_at
    OR (event.action='cancel' AND (run.status IS DISTINCT FROM 'cancelled'
                                  OR run.completed_at IS DISTINCT FROM event.created_at)) THEN
    RAISE EXCEPTION '人工等待事件缺少匹配成功审计或最终运行状态';
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER human_wait_events_state AFTER INSERT ON investigation_human_wait_events
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_human_wait_event_state();
"""


_HANDOFF_STATE = """
CREATE OR REPLACE FUNCTION validate_terminal_handoff_state() RETURNS trigger AS $$
DECLARE
  run_id UUID;
  run investigation_runs%ROWTYPE;
  handoff investigation_terminal_handoffs%ROWTYPE;
  parent investigation_attempts%ROWTYPE;
  work work_items%ROWTYPE;
  cancellation investigation_human_wait_events%ROWTYPE;
  expected_status TEXT;
BEGIN
  IF TG_TABLE_NAME='investigation_runs' THEN run_id:=NEW.id;
  ELSE run_id:=NEW.investigation_run_id; END IF;
  SELECT * INTO run FROM investigation_runs WHERE id=run_id;
  SELECT * INTO handoff FROM investigation_terminal_handoffs WHERE investigation_run_id=run_id;
  IF handoff.id IS NULL THEN
    IF run.status IN ('await_human','await_candidate','cancelled') OR EXISTS
      (SELECT 1 FROM investigation_attempts
       WHERE investigation_run_id=run_id AND status='handed_off')
    THEN RAISE EXCEPTION '等待状态或交接尝试缺少终端交接事实'; END IF;
    RETURN NULL;
  END IF;
  SELECT * INTO parent FROM investigation_attempts WHERE id=handoff.investigation_attempt_id;
  SELECT * INTO work FROM work_items WHERE investigation_run_id=run_id;
  SELECT * INTO cancellation FROM investigation_human_wait_events
    WHERE investigation_run_id=run_id ORDER BY state_version DESC LIMIT 1;
  expected_status:=CASE handoff.destination WHEN 'await_human' THEN 'await_human'
                       ELSE 'await_candidate' END;
  IF run.status='cancelled' THEN
    IF handoff.destination<>'await_human' OR cancellation.action IS DISTINCT FROM 'cancel'
      OR run.completed_at IS DISTINCT FROM cancellation.created_at THEN
      RAISE EXCEPTION '取消运行缺少最终可信人工取消事件';
    END IF;
  ELSIF run.status IS DISTINCT FROM expected_status OR run.completed_at IS NOT NULL
    OR cancellation.action='cancel' THEN
    RAISE EXCEPTION '终端交接与运行、任务、父尝试状态不一致';
  END IF;
  IF run.result IS NOT NULL OR run.metrics IS NOT NULL OR work.id IS NULL OR parent.id IS NULL
    OR work.status<>'completed' OR work.completed_at IS DISTINCT FROM handoff.created_at
    OR work.locked_by IS NOT NULL OR work.locked_until IS NOT NULL OR work.lease_token IS NOT NULL
    OR parent.status<>'handed_off' OR parent.work_item_id IS DISTINCT FROM work.id
    OR parent.finished_at IS DISTINCT FROM handoff.created_at
    OR EXISTS (SELECT 1 FROM investigation_attempts WHERE investigation_run_id=run_id
               AND status='handed_off' AND id<>handoff.investigation_attempt_id) THEN
    RAISE EXCEPTION '终端交接与运行、任务、父尝试状态不一致';
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM investigation_human_wait_events)
          OR EXISTS (SELECT 1 FROM investigation_human_wait_commands)
          OR EXISTS (SELECT 1 FROM investigation_runs WHERE status='cancelled')
          OR EXISTS (SELECT 1 FROM agent_task_events
                     WHERE event_type IN ('run.human_input_saved','run.cancelled')) THEN
          RAISE EXCEPTION '不能删除已保存的人工等待事实或命令审计';
        END IF;
      END $$;
      DROP TRIGGER human_wait_events_state ON investigation_human_wait_events;
      DROP FUNCTION validate_human_wait_event_state();
      DROP TRIGGER human_wait_commands_source ON investigation_human_wait_commands;
      DROP FUNCTION validate_human_wait_command_insert();
      DROP TRIGGER human_wait_events_source ON investigation_human_wait_events;
      DROP FUNCTION validate_human_wait_event_insert();
      DROP TRIGGER human_wait_events_immutable ON investigation_human_wait_events;
      DROP TRIGGER human_wait_commands_immutable ON investigation_human_wait_commands;
      DROP FUNCTION reject_human_wait_mutation();
    """)
    op.execute(_ORIGINAL_HANDOFF_STATE)
    op.drop_constraint(
        "fk_human_wait_events_command", "investigation_human_wait_events", type_="foreignkey"
    )
    op.drop_table("investigation_human_wait_commands")
    op.drop_table("investigation_human_wait_events")
    op.drop_constraint(
        "uq_terminal_handoffs_id_run", "investigation_terminal_handoffs", type_="unique"
    )
    op.drop_constraint("ck_investigation_runs_cancelled_scope", "investigation_runs", type_="check")
    _lifecycle(enabled=False)


_ORIGINAL_HANDOFF_STATE = """
      CREATE OR REPLACE FUNCTION validate_terminal_handoff_state() RETURNS trigger AS $$
      DECLARE
        run_id UUID;
        run investigation_runs%ROWTYPE;
        handoff investigation_terminal_handoffs%ROWTYPE;
        parent investigation_attempts%ROWTYPE;
        work work_items%ROWTYPE;
        expected_status TEXT;
      BEGIN
        IF TG_TABLE_NAME = 'investigation_runs' THEN
          run_id := NEW.id;
        ELSE
          run_id := NEW.investigation_run_id;
        END IF;
        SELECT * INTO run FROM investigation_runs WHERE id = run_id;
        SELECT * INTO handoff FROM investigation_terminal_handoffs
          WHERE investigation_run_id = run_id;
        IF handoff.id IS NULL THEN
          IF run.status IN ('await_human', 'await_candidate') OR EXISTS
             (SELECT 1 FROM investigation_attempts
              WHERE investigation_run_id = run_id AND status = 'handed_off') THEN
            RAISE EXCEPTION '等待状态或交接尝试缺少终端交接事实';
          END IF;
          RETURN NULL;
        END IF;
        SELECT * INTO parent FROM investigation_attempts
          WHERE id = handoff.investigation_attempt_id;
        SELECT * INTO work FROM work_items WHERE investigation_run_id = run_id;
        expected_status := CASE handoff.destination
          WHEN 'await_human' THEN 'await_human' ELSE 'await_candidate' END;
        IF run.status IS DISTINCT FROM expected_status OR run.completed_at IS NOT NULL
           OR run.result IS NOT NULL OR run.metrics IS NOT NULL
           OR work.id IS NULL OR parent.id IS NULL OR work.status <> 'completed'
           OR work.completed_at IS DISTINCT FROM handoff.created_at
           OR work.locked_by IS NOT NULL OR work.locked_until IS NOT NULL
           OR work.lease_token IS NOT NULL OR parent.status <> 'handed_off'
           OR parent.work_item_id IS DISTINCT FROM work.id
           OR parent.finished_at IS DISTINCT FROM handoff.created_at
           OR EXISTS (SELECT 1 FROM investigation_attempts
                      WHERE investigation_run_id = run_id AND status = 'handed_off'
                        AND id <> handoff.investigation_attempt_id) THEN
          RAISE EXCEPTION '终端交接与运行、任务、父尝试状态不一致';
        END IF;
        RETURN NULL;
      END;
      $$ LANGUAGE plpgsql;
"""
