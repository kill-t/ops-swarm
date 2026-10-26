"""远端发布固定窗口、取消隔离及同事务应用闭环。"""

from alembic import op

revision = "0081_lab_release_dispatch"
down_revision = "0080_candidate_release_request"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(_GUARDS)


_TABLES = """
CREATE TABLE lab_release_dispatches (
 operation_id UUID NOT NULL PRIMARY KEY,
 policy_version VARCHAR(32) NOT NULL,
 command JSONB NOT NULL,
 command_sha256 VARCHAR(64) NOT NULL,
 not_before TIMESTAMP WITH TIME ZONE NOT NULL,
 expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
 status VARCHAR(16) NOT NULL,
 command_id UUID REFERENCES lab_release_commands(id) ON DELETE RESTRICT,
 created_at TIMESTAMP WITH TIME ZONE NOT NULL,
 finished_at TIMESTAMP WITH TIME ZONE,
 CONSTRAINT uq_lab_release_dispatch_command UNIQUE(command_id),
 CONSTRAINT ck_lab_dispatch_policy CHECK(policy_version='lab-release-dispatch-v1'),
 CONSTRAINT ck_lab_dispatch_hash CHECK(command_sha256 ~ '^[0-9a-f]{64}$'),
 CONSTRAINT ck_lab_dispatch_body CHECK(jsonb_typeof(command)='object'
   AND octet_length(command::text)<=4096),
 CONSTRAINT ck_lab_dispatch_window CHECK(not_before<=created_at AND created_at<expires_at
   AND expires_at-not_before<=interval '30 seconds'),
 CONSTRAINT ck_lab_dispatch_status CHECK(
   (status='prepared' AND command_id IS NULL AND finished_at IS NULL) OR
   (status='applied' AND command_id IS NOT NULL AND finished_at>=created_at
     AND finished_at<expires_at) OR
   (status='cancelled' AND command_id IS NULL AND finished_at>=created_at))
);
"""

_GUARDS = """
CREATE FUNCTION assert_lab_release_dispatch(value lab_release_dispatches) RETURNS void AS $$
DECLARE body JSONB;
BEGIN
 body:=value.command;
 IF (value.policy_version='lab-release-dispatch-v1'
   AND isfinite(value.not_before) AND isfinite(value.expires_at) AND isfinite(value.created_at)
   AND value.not_before<=value.created_at AND value.created_at<value.expires_at
   AND value.expires_at-value.not_before<=interval '30 seconds'
   AND jsonb_typeof(body)='object'
   AND body ?& ARRAY['expected_version','expected_revision','target_version','idempotency_key']
   AND body-ARRAY['expected_version','expected_revision','target_version','idempotency_key']='{}'
   AND body->>'expected_version'='2026.01.01-rc1' AND body->>'target_version'='stable'
   AND jsonb_typeof(body->'expected_revision')='number'
   AND body->>'expected_revision' ~ '^[1-9][0-9]*$'
   AND (body->>'expected_revision')::numeric BETWEEN 1 AND 2147483647
   AND body->>'idempotency_key'='candidate-release-'||value.operation_id::text
   AND value.command_sha256=encode(sha256(convert_to(candidate_canonical_json(body),'UTF8')),'hex')
   AND (value.finished_at IS NULL OR isfinite(value.finished_at))) IS NOT TRUE THEN
   RAISE EXCEPTION '远端派发缺少固定原命令、摘要或有界窗口';
 END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION guard_lab_release_dispatch() RETURNS trigger AS $$
DECLARE applied lab_release_commands%ROWTYPE;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION '远端派发事实不可删除'; END IF;
 -- 所有写入口先锁头再锁操作；不得反向等待执行命令的头锁。
 PERFORM 1 FROM lab_release_states WHERE service_name='order-service' FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION '派发要求已有发布演练状态'; END IF;
 PERFORM assert_lab_release_dispatch(NEW);
 IF TG_OP='INSERT' THEN
   IF (NEW.status='prepared' AND NEW.command_id IS NULL AND NEW.finished_at IS NULL
     AND NEW.created_at<=clock_timestamp() AND NEW.not_before<=clock_timestamp()
     AND clock_timestamp()<NEW.expires_at AND NOT EXISTS(
       SELECT 1 FROM lab_release_commands WHERE idempotency_key=NEW.command->>'idempotency_key'
     )) IS NOT TRUE THEN RAISE EXCEPTION '派发只能在有效窗口注册为prepared'; END IF;
 ELSE
   PERFORM assert_lab_release_dispatch(OLD);
   IF (to_jsonb(NEW)-ARRAY['status','command_id','finished_at']) IS DISTINCT FROM
      (to_jsonb(OLD)-ARRAY['status','command_id','finished_at'])
      OR OLD.status<>'prepared' THEN RAISE EXCEPTION '派发原文、窗口及终态不可改写'; END IF;
   IF NEW.status='cancelled' THEN
     IF (NEW.command_id IS NULL AND NEW.finished_at>=NEW.created_at
       AND NEW.finished_at<=clock_timestamp()) IS NOT TRUE THEN
       RAISE EXCEPTION '取消必须保存真实终止时刻'; END IF;
   ELSIF NEW.status='applied' THEN
     SELECT * INTO applied FROM lab_release_commands WHERE id=NEW.command_id;
     IF (pg_trigger_depth()>=2 AND applied.id IS NOT NULL AND applied.kind='cas'
       AND applied.command=NEW.command AND applied.command_sha256=NEW.command_sha256
       AND applied.created_at=NEW.finished_at AND applied.created_at>=NEW.created_at
       AND applied.created_at>=NEW.not_before AND applied.created_at<NEW.expires_at
       AND clock_timestamp()<NEW.expires_at) IS NOT TRUE THEN
       RAISE EXCEPTION '派发只能由窗口内同源命令原子应用'; END IF;
   ELSE RAISE EXCEPTION '派发只允许应用或取消'; END IF;
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER lab_release_dispatch_guard BEFORE INSERT OR UPDATE OR DELETE
 ON lab_release_dispatches
 FOR EACH ROW EXECUTE FUNCTION guard_lab_release_dispatch();

CREATE FUNCTION guard_lab_dispatch_command() RETURNS trigger AS $$
DECLARE dispatch lab_release_dispatches%ROWTYPE;
BEGIN
 IF NEW.idempotency_key ~
 '^candidate-release-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
   SELECT * INTO dispatch FROM lab_release_dispatches
     WHERE operation_id=right(NEW.idempotency_key,36)::uuid FOR UPDATE;
   IF dispatch.operation_id IS NULL THEN RAISE EXCEPTION '候选命令缺少持久派发窗口'; END IF;
   PERFORM assert_lab_release_dispatch(dispatch);
   IF (dispatch.status='prepared' AND NEW.kind='cas' AND NEW.command=dispatch.command
     AND NEW.command_sha256=dispatch.command_sha256 AND NEW.created_at>=dispatch.created_at
     AND dispatch.not_before<=NEW.created_at AND NEW.created_at<dispatch.expires_at
     AND NEW.created_at<=clock_timestamp() AND dispatch.not_before<=clock_timestamp()
     AND clock_timestamp()<dispatch.expires_at) IS NOT TRUE THEN
     RAISE EXCEPTION '候选命令已取消、越期或改变原命令'; END IF;
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER lab_release_dispatch_command_guard BEFORE INSERT ON lab_release_commands
 FOR EACH ROW EXECUTE FUNCTION guard_lab_dispatch_command();

CREATE FUNCTION apply_lab_dispatch_command() RETURNS trigger AS $$
BEGIN
 IF NEW.idempotency_key ~
 '^candidate-release-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
   UPDATE lab_release_dispatches SET status='applied',command_id=NEW.id,finished_at=NEW.created_at
     WHERE operation_id=right(NEW.idempotency_key,36)::uuid;
 END IF;
 RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER lab_release_dispatch_command_apply AFTER INSERT ON lab_release_commands
 FOR EACH ROW EXECUTE FUNCTION apply_lab_dispatch_command();

CREATE FUNCTION validate_lab_dispatch_commit() RETURNS trigger AS $$
DECLARE dispatch lab_release_dispatches%ROWTYPE; command_tx XID; dispatch_tx XID;
BEGIN
 IF NEW.idempotency_key ~
 '^candidate-release-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
   SELECT * INTO dispatch FROM lab_release_dispatches
     WHERE operation_id=right(NEW.idempotency_key,36)::uuid;
   IF (dispatch.status='applied' AND dispatch.command_id=NEW.id AND dispatch.command=NEW.command
     AND dispatch.command_sha256=NEW.command_sha256 AND dispatch.finished_at=NEW.created_at
     AND clock_timestamp()<dispatch.expires_at) IS NOT TRUE THEN
     RAISE EXCEPTION '派发与成功命令缺少截止前同事务闭环'; END IF;
   SELECT xmin INTO command_tx FROM lab_release_commands WHERE id=NEW.id;
   SELECT xmin INTO dispatch_tx FROM lab_release_dispatches
     WHERE operation_id=dispatch.operation_id;
   IF command_tx IS DISTINCT FROM dispatch_tx THEN
     RAISE EXCEPTION '派发与命令必须同事务应用'; END IF;
 END IF;
 RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER lab_release_dispatch_commit AFTER INSERT ON lab_release_commands
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_lab_dispatch_commit();
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN IF EXISTS(SELECT 1 FROM lab_release_dispatches) THEN
        RAISE EXCEPTION '不能删除已保存的远端派发事实'; END IF; END $$;
      DROP TRIGGER lab_release_dispatch_commit ON lab_release_commands;
      DROP FUNCTION validate_lab_dispatch_commit();
      DROP TRIGGER lab_release_dispatch_command_apply ON lab_release_commands;
      DROP FUNCTION apply_lab_dispatch_command();
      DROP TRIGGER lab_release_dispatch_command_guard ON lab_release_commands;
      DROP FUNCTION guard_lab_dispatch_command();
      DROP TRIGGER lab_release_dispatch_guard ON lab_release_dispatches;
      DROP FUNCTION guard_lab_release_dispatch();
      DROP FUNCTION assert_lab_release_dispatch(lab_release_dispatches);
      DROP TABLE lab_release_dispatches;
    """)
