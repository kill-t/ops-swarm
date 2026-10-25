"""持久化发布演练状态、有限CAS及不可变命令。"""

from alembic import op

revision = "0079_lab_release_state"
down_revision = "0078_condition_approval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(_GUARDS)


_TABLES = """

CREATE TABLE lab_release_states (
  service_name VARCHAR(50) NOT NULL,
  version VARCHAR(40) NOT NULL,
  revision INTEGER NOT NULL,
  command_count INTEGER NOT NULL,
  last_command_id UUID,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
  PRIMARY KEY (service_name),
  CONSTRAINT ck_lab_release_state_service CHECK (service_name='order-service'),
  CONSTRAINT ck_lab_release_state_version CHECK (version IN ('stable','2026.01.01-rc1')),
  CONSTRAINT ck_lab_release_state_counters CHECK (revision>=1 AND command_count>=0),
  CONSTRAINT ck_lab_release_state_head CHECK ((command_count=0) = (last_command_id IS NULL))
)

;

CREATE TABLE lab_release_commands (
  id UUID NOT NULL,
  service_name VARCHAR(50) NOT NULL,
  sequence INTEGER NOT NULL,
  kind VARCHAR(24) NOT NULL,
  idempotency_key VARCHAR(100) NOT NULL,
  command JSONB NOT NULL,
  command_sha256 VARCHAR(64) NOT NULL,
  previous_version VARCHAR(40) NOT NULL,
  previous_revision INTEGER NOT NULL,
  version VARCHAR(40) NOT NULL,
  revision INTEGER NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL,
  PRIMARY KEY (id),
  CONSTRAINT uq_lab_release_command_sequence UNIQUE (service_name, sequence),
  CONSTRAINT uq_lab_release_command_key UNIQUE (service_name, idempotency_key),
  CONSTRAINT ck_lab_release_command_kind CHECK (kind IN ('cas','legacy_fault_switch')),
  CONSTRAINT ck_lab_release_command_revision CHECK (sequence>=1 AND previous_revision>=1
  AND revision>=1),
  CONSTRAINT ck_lab_release_command_version CHECK (previous_version IN
  ('stable','2026.01.01-rc1') AND version IN ('stable','2026.01.01-rc1')),
  CONSTRAINT ck_lab_release_command_hash CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT ck_lab_release_command_body CHECK (jsonb_typeof(command)='object' AND
  octet_length(command::text)<=4096),
  CONSTRAINT ck_lab_release_command_key CHECK (char_length(idempotency_key) BETWEEN 8 AND
  100 AND idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'),
  FOREIGN KEY(service_name) REFERENCES lab_release_states (service_name) ON DELETE RESTRICT
)

;
ALTER TABLE lab_release_states ADD CONSTRAINT fk_lab_release_state_command FOREIGN
  KEY(last_command_id) REFERENCES lab_release_commands (id) ON DELETE RESTRICT;
"""

_GUARDS = """
CREATE FUNCTION guard_lab_release_state() RETURNS trigger AS $$
DECLARE command lab_release_commands%ROWTYPE;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION '发布演练状态不可删除'; END IF;
 IF TG_OP='INSERT' THEN
   IF (NEW.service_name='order-service' AND NEW.version='stable' AND NEW.revision=1
     AND NEW.command_count=0 AND NEW.last_command_id IS NULL) IS NOT TRUE THEN
     RAISE EXCEPTION '发布演练只能从固定初态初始化'; END IF;
   RETURN NEW;
 END IF;
 -- 保存点内xmin属于子事务；校验自动推进入口与精确相邻命令，不误认顶层事务号。
 IF pg_trigger_depth()<2 THEN
   RAISE EXCEPTION '发布演练状态只能由同事务相邻命令推进'; END IF;
 SELECT * INTO command FROM lab_release_commands WHERE id=NEW.last_command_id;
 IF (command.id IS NOT NULL AND NEW.service_name=OLD.service_name
   AND command.service_name=OLD.service_name AND command.sequence=OLD.command_count+1
   AND command.previous_version=OLD.version AND command.previous_revision=OLD.revision
   AND command.created_at>=OLD.updated_at AND NEW.version=command.version
   AND NEW.revision=command.revision AND NEW.command_count=command.sequence
   AND NEW.updated_at=command.created_at) IS NOT TRUE THEN
   RAISE EXCEPTION '发布演练状态只能由同事务相邻命令推进'; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER lab_release_state_guard BEFORE INSERT OR UPDATE OR DELETE ON lab_release_states
 FOR EACH ROW EXECUTE FUNCTION guard_lab_release_state();

CREATE FUNCTION guard_lab_release_command() RETURNS trigger AS $$
DECLARE state lab_release_states%ROWTYPE; target TEXT; body JSONB;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '发布演练命令不可更新或删除'; END IF;
 SELECT * INTO state FROM lab_release_states WHERE service_name=NEW.service_name FOR UPDATE;
 body:=NEW.command;
 IF NEW.kind='cas' THEN
   IF (jsonb_typeof(body)='object'
     AND body ?& ARRAY['expected_version','expected_revision','target_version','idempotency_key']
     AND body-ARRAY['expected_version','expected_revision','target_version','idempotency_key']='{}'
     AND jsonb_typeof(body->'expected_version')='string'
     AND jsonb_typeof(body->'target_version')='string'
     AND jsonb_typeof(body->'expected_revision')='number'
     AND body->>'expected_revision' ~ '^[1-9][0-9]*$'
     AND body->>'expected_version'=state.version
     AND body->'expected_revision'=to_jsonb(state.revision)) IS NOT TRUE THEN
     RAISE EXCEPTION '发布演练CAS命令与当前版本修订不一致'; END IF;
   target:=body->>'target_version';
 ELSIF NEW.kind='legacy_fault_switch' THEN
   IF (jsonb_typeof(body)='object' AND body ?& ARRAY['enabled','idempotency_key']
     AND body-ARRAY['enabled','idempotency_key']='{}'
     AND jsonb_typeof(body->'enabled')='boolean') IS NOT TRUE THEN
     RAISE EXCEPTION '旧开关必须绑定严格布尔命令'; END IF;
   target:=CASE body->>'enabled' WHEN 'true' THEN '2026.01.01-rc1' ELSE 'stable' END;
 ELSE RAISE EXCEPTION '发布演练命令种类不合法';
 END IF;
 IF (state.service_name='order-service' AND target IN ('stable','2026.01.01-rc1')
   AND jsonb_typeof(body->'idempotency_key')='string'
   AND body->>'idempotency_key'=NEW.idempotency_key
   AND NEW.command_sha256=encode(sha256(convert_to(candidate_canonical_json(body),'UTF8')),'hex')
   AND NEW.sequence=state.command_count+1 AND NEW.previous_version=state.version
   AND NEW.previous_revision=state.revision AND NEW.version=target
   AND NEW.revision=state.revision+(target<>state.version)::int
   AND NEW.created_at>=state.updated_at) IS NOT TRUE THEN
   RAISE EXCEPTION '发布演练命令缺少精确前态、摘要或相邻修订'; END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER lab_release_command_guard BEFORE INSERT OR UPDATE OR DELETE ON
  lab_release_commands
 FOR EACH ROW EXECUTE FUNCTION guard_lab_release_command();

CREATE FUNCTION apply_lab_release_command() RETURNS trigger AS $$
BEGIN
 UPDATE lab_release_states SET version=NEW.version,revision=NEW.revision,
   command_count=NEW.sequence,last_command_id=NEW.id,updated_at=NEW.created_at
   WHERE service_name=NEW.service_name;
 RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER lab_release_command_apply AFTER INSERT ON lab_release_commands
 FOR EACH ROW EXECUTE FUNCTION apply_lab_release_command();
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM lab_release_states) OR EXISTS (SELECT 1 FROM
  lab_release_commands)
          THEN RAISE EXCEPTION '不能删除已保存的发布演练状态或命令'; END IF;
      END $$;
      DROP TRIGGER lab_release_command_apply ON lab_release_commands;
      DROP FUNCTION apply_lab_release_command();
      DROP TRIGGER lab_release_command_guard ON lab_release_commands;
      DROP FUNCTION guard_lab_release_command();
      DROP TRIGGER lab_release_state_guard ON lab_release_states;
      DROP FUNCTION guard_lab_release_state();
      ALTER TABLE lab_release_states DROP CONSTRAINT fk_lab_release_state_command;
      DROP TABLE lab_release_commands;
      DROP TABLE lab_release_states;
    """)
