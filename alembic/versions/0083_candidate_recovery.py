"""候选发布恢复验证的固定窗口、逐次样本与不可变终态闭环。"""

from alembic import op

revision = "0083_candidate_recovery"
down_revision = "0082_candidate_release_job"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(_TABLES)
    op.execute(_SOURCE)
    op.execute(_GUARDS)
    op.execute(_RESULTS)
    op.execute(_STATE)


_TABLES = """
CREATE TABLE candidate_recovery_jobs (
 id UUID PRIMARY KEY REFERENCES candidate_release_execution_jobs(id) ON DELETE RESTRICT,
 investigation_run_id UUID NOT NULL REFERENCES investigation_runs(id) ON DELETE RESTRICT,
 candidate_job_id UUID NOT NULL REFERENCES investigation_candidate_evaluation_jobs(id)
   ON DELETE RESTRICT,
 verification_version INTEGER NOT NULL,
 operation_id UUID NOT NULL,
 execution_result_sha256 VARCHAR(64) NOT NULL,
 policy_version VARCHAR(40) NOT NULL,
 window_start TIMESTAMPTZ NOT NULL,
 window_end TIMESTAMPTZ NOT NULL,
 status VARCHAR(16) NOT NULL,
 attempt_count INTEGER NOT NULL,
 locked_by VARCHAR(100),
 lease_token UUID,
 locked_until TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL,
 completed_at TIMESTAMPTZ,
 CONSTRAINT ck_recovery_job_policy CHECK (policy_version='candidate-recovery-v1'),
 CONSTRAINT ck_recovery_job_counts CHECK (verification_version BETWEEN 1 AND 8 AND
   attempt_count BETWEEN 0 AND 3),
 CONSTRAINT ck_recovery_job_status CHECK (status IN ('queued','running','completed')),
 CONSTRAINT ck_recovery_job_hash CHECK (execution_result_sha256 ~ '^[0-9a-f]{64}$'),
 CONSTRAINT ck_recovery_job_lease CHECK ((status='running' AND locked_by IS NOT NULL AND
   lease_token IS NOT NULL AND locked_until IS NOT NULL AND completed_at IS NULL) OR
   (status<>'running' AND locked_by IS NULL AND lease_token IS NULL AND locked_until IS NULL
   AND ((status='queued' AND completed_at IS NULL) OR
   (status='completed' AND completed_at IS NOT NULL))))
);
CREATE INDEX ix_recovery_job_claim ON candidate_recovery_jobs(status,locked_until,created_at);
CREATE TABLE candidate_recovery_attempts (
 id UUID PRIMARY KEY,
 job_id UUID NOT NULL REFERENCES candidate_recovery_jobs(id) ON DELETE RESTRICT,
 number INTEGER NOT NULL,
 lease_token UUID NOT NULL,
 worker_id VARCHAR(100) NOT NULL,
 status VARCHAR(16) NOT NULL,
 started_at TIMESTAMPTZ NOT NULL,
 lease_expires_at TIMESTAMPTZ NOT NULL,
 finished_at TIMESTAMPTZ,
 CONSTRAINT uq_recovery_attempt_number UNIQUE(job_id,number),
 CONSTRAINT uq_recovery_attempt_token UNIQUE(lease_token),
 CONSTRAINT ck_recovery_attempt_lease CHECK (number BETWEEN 1 AND 3 AND
   lease_expires_at=started_at+interval '30 seconds'),
 CONSTRAINT ck_recovery_attempt_status CHECK ((status='running' AND finished_at IS NULL) OR
   (status IN ('succeeded','failed','lease_expired') AND finished_at IS NOT NULL AND
   finished_at>=started_at))
);
CREATE TABLE candidate_recovery_samples (
 attempt_id UUID NOT NULL REFERENCES candidate_recovery_attempts(id) ON DELETE RESTRICT,
 sequence INTEGER NOT NULL,
 job_id UUID NOT NULL REFERENCES candidate_recovery_jobs(id) ON DELETE RESTRICT,
 observation JSONB,
 error_code VARCHAR(100),
 created_at TIMESTAMPTZ NOT NULL,
 PRIMARY KEY(attempt_id,sequence),
 CONSTRAINT ck_recovery_sample_sequence CHECK (sequence BETWEEN 1 AND 3),
 CONSTRAINT ck_recovery_sample_content CHECK ((observation IS NOT NULL AND error_code IS NULL
   AND jsonb_typeof(observation)='object' AND octet_length(observation::text)<=4096) OR
   (observation IS NULL AND error_code IS NOT NULL AND char_length(error_code)>0))
);
CREATE TABLE candidate_recovery_results (
 job_id UUID PRIMARY KEY REFERENCES candidate_recovery_jobs(id) ON DELETE RESTRICT,
 content JSONB NOT NULL,
 content_sha256 VARCHAR(64) NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 CONSTRAINT ck_recovery_result_content CHECK (jsonb_typeof(content)='object' AND
   octet_length(content::text)<=8192 AND content_sha256 ~ '^[0-9a-f]{64}$')
);
"""


_SOURCE = """
CREATE FUNCTION candidate_recovery_utc_text(value TIMESTAMPTZ) RETURNS TEXT AS $$
 SELECT CASE WHEN isfinite(value) THEN
   to_char(value AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS') ||
   CASE WHEN extract(microseconds FROM value)::bigint % 1000000=0 THEN ''
     ELSE '.' || to_char(value AT TIME ZONE 'UTC','US') END || 'Z'
   ELSE NULL END;
$$ LANGUAGE SQL IMMUTABLE STRICT;

CREATE FUNCTION assert_recovery_source(job candidate_recovery_jobs) RETURNS TEXT AS $$
DECLARE original candidate_release_execution_jobs%ROWTYPE;
 result candidate_release_execution_results%ROWTYPE; run investigation_runs%ROWTYPE;
BEGIN
 SELECT * INTO original FROM candidate_release_execution_jobs WHERE id=job.id;
 IF original.id IS NULL OR original.status NOT IN ('succeeded','failed','unknown') THEN
   RAISE EXCEPTION '恢复验证缺少终态发布执行来源'; END IF;
 -- 重建原准入和124/127全部历史，不把当年的写授权当作当前执行资格。
 PERFORM assert_release_job_history(original);
 SELECT * INTO result FROM candidate_release_execution_results WHERE job_id=original.id;
 SELECT * INTO run FROM investigation_runs WHERE id=original.investigation_run_id;
 IF (job.investigation_run_id=original.investigation_run_id
   AND job.candidate_job_id=original.candidate_job_id
   AND job.verification_version=original.verification_version
   AND job.operation_id=original.operation_id
   AND job.execution_result_sha256=result.content_sha256
   AND result.content_sha256=encode(sha256(convert_to(
     candidate_canonical_json(result.content),'UTF8')),'hex')
   AND job.window_start=original.completed_at AND run.started_at IS NOT NULL
   AND jsonb_typeof(run.call_budget->'max_duration_seconds')='number'
   AND run.call_budget->>'max_duration_seconds' ~ '^[1-9][0-9]*$'
   AND job.window_end=least(original.completed_at+interval '120 seconds',
     run.started_at+(run.call_budget->>'max_duration_seconds')::integer*interval '1 second')
   AND isfinite(job.created_at) AND isfinite(job.updated_at)
   AND isfinite(job.window_start) AND isfinite(job.window_end)
   AND job.created_at>=original.completed_at AND job.updated_at>=job.created_at)
   IS NOT TRUE THEN RAISE EXCEPTION '恢复验证来源、摘要、范围或固定窗口不一致'; END IF;
 RETURN result.content->>'outcome';
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION lock_recovery_parent(identifier UUID) RETURNS VOID AS $$
BEGIN
 PERFORM 1 FROM work_items WHERE investigation_run_id=(SELECT investigation_run_id
   FROM candidate_release_execution_jobs WHERE id=identifier) FOR UPDATE;
END;
$$ LANGUAGE plpgsql;
"""


_GUARDS = """
CREATE FUNCTION guard_recovery_job() RETURNS trigger AS $$
DECLARE source TEXT; attempt candidate_recovery_attempts%ROWTYPE;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION '恢复验证任务不可删除'; END IF;
 PERFORM lock_recovery_parent(NEW.id);
 source:=assert_recovery_source(NEW);
 -- 宿主与容器仅允许有界时钟偏差；所有租约和窗口截止仍使用严格数据库时钟。
 IF NEW.updated_at>clock_timestamp()+interval '1 second' THEN
   RAISE EXCEPTION '恢复验证任务不能保存未来时刻'; END IF;
 IF TG_OP='INSERT' THEN
   IF (NEW.status='queued' AND NEW.attempt_count=0 AND NEW.created_at=NEW.updated_at)
     IS NOT TRUE THEN RAISE EXCEPTION '恢复验证只能从queued创建'; END IF;
 ELSE
   IF (to_jsonb(NEW)-ARRAY['status','attempt_count','locked_by','lease_token','locked_until',
     'updated_at','completed_at']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','attempt_count',
     'locked_by','lease_token','locked_until','updated_at','completed_at']) OR
     OLD.status='completed' OR NEW.updated_at<OLD.updated_at THEN
     RAISE EXCEPTION '恢复验证身份、固定窗口及终态不可改写'; END IF;
   IF NEW.status='running' THEN
     IF (source='applied_current' AND OLD.attempt_count<3 AND
       NEW.attempt_count=OLD.attempt_count+1 AND
       NEW.lease_token IS DISTINCT FROM OLD.lease_token AND
       (OLD.status='queued' OR (OLD.status='running' AND OLD.locked_until<=NEW.updated_at
         AND OLD.locked_until<=clock_timestamp()))
       AND NEW.locked_until=NEW.updated_at+interval '30 seconds'
       AND NEW.updated_at>=NEW.window_start AND NEW.updated_at<NEW.window_end
       AND clock_timestamp()<NEW.window_end AND clock_timestamp()<NEW.locked_until
       AND char_length(NEW.locked_by)>0) IS NOT TRUE THEN
       RAISE EXCEPTION '恢复验证领取需要可用任务及固定30秒新租约'; END IF;
   ELSIF NEW.status='completed' THEN
     IF (OLD.status IN ('queued','running') AND NEW.attempt_count=OLD.attempt_count
       AND NEW.completed_at=NEW.updated_at) IS NOT TRUE THEN
       RAISE EXCEPTION '恢复验证终态必须保留尝试身份与完成时刻'; END IF;
     IF OLD.status='running' THEN
       SELECT * INTO attempt FROM candidate_recovery_attempts
         WHERE job_id=OLD.id AND number=OLD.attempt_count;
       IF (attempt.lease_token=OLD.lease_token AND attempt.worker_id=OLD.locked_by AND
         ((attempt.status IN ('succeeded','failed') AND
           NEW.updated_at<least(OLD.locked_until,NEW.window_end) AND
           clock_timestamp()<least(OLD.locked_until,NEW.window_end)) OR
         (attempt.status='lease_expired' AND (NEW.updated_at>=NEW.window_end OR
           (OLD.attempt_count=3 AND NEW.updated_at>=OLD.locked_until))))) IS NOT TRUE THEN
         RAISE EXCEPTION '恢复验证终态需要当前租约或耗尽收敛'; END IF;
     END IF;
   ELSE RAISE EXCEPTION '恢复验证状态迁移不合法'; END IF;
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER recovery_job_guard BEFORE INSERT OR UPDATE OR DELETE ON candidate_recovery_jobs
 FOR EACH ROW EXECUTE FUNCTION guard_recovery_job();

CREATE FUNCTION guard_recovery_attempt() RETURNS trigger AS $$
DECLARE job candidate_recovery_jobs%ROWTYPE;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION '恢复验证尝试不可删除'; END IF;
 PERFORM lock_recovery_parent(NEW.job_id);
 SELECT * INTO job FROM candidate_recovery_jobs WHERE id=NEW.job_id FOR UPDATE;
 PERFORM assert_recovery_source(job);
 IF NEW.started_at>clock_timestamp()+interval '1 second' OR
   NEW.finished_at>clock_timestamp()+interval '1 second' THEN
   RAISE EXCEPTION '恢复验证尝试不能保存未来时刻'; END IF;
 IF TG_OP='INSERT' THEN
   IF (NEW.status='running' AND job.status='running' AND NEW.number=job.attempt_count
     AND NEW.lease_token=job.lease_token AND NEW.worker_id=job.locked_by
     AND NEW.started_at=job.updated_at AND NEW.lease_expires_at=job.locked_until
     AND clock_timestamp()<least(NEW.lease_expires_at,job.window_end)) IS NOT TRUE THEN
     RAISE EXCEPTION '恢复验证尝试必须绑定当前任务租约'; END IF;
 ELSE
   IF (to_jsonb(NEW)-ARRAY['status','finished_at']) IS DISTINCT FROM
     (to_jsonb(OLD)-ARRAY['status','finished_at']) OR OLD.status<>'running'
     OR NEW.status='running' THEN RAISE EXCEPTION '恢复验证尝试原文及终态不可改写'; END IF;
   IF (job.status='running' AND NEW.number=job.attempt_count
     AND NEW.lease_token=job.lease_token AND NEW.worker_id=job.locked_by
     AND isfinite(NEW.finished_at)) IS NOT TRUE THEN
     RAISE EXCEPTION '恢复验证旧尝试不能结算'; END IF;
   IF NEW.status='lease_expired' THEN
     IF (NEW.finished_at>=least(NEW.lease_expires_at,job.window_end) AND
       clock_timestamp()>=least(NEW.lease_expires_at,job.window_end)) IS NOT TRUE THEN
       RAISE EXCEPTION '恢复验证尝试尚未过期'; END IF;
   ELSIF (NEW.finished_at<least(NEW.lease_expires_at,job.window_end) AND
     clock_timestamp()<least(NEW.lease_expires_at,job.window_end)) IS NOT TRUE THEN
     RAISE EXCEPTION '迟到恢复验证尝试不能结算'; END IF;
 END IF;
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER recovery_attempt_guard BEFORE INSERT OR UPDATE OR DELETE
 ON candidate_recovery_attempts FOR EACH ROW EXECUTE FUNCTION guard_recovery_attempt();

CREATE FUNCTION assert_recovery_sample(sample candidate_recovery_samples) RETURNS VOID AS $$
DECLARE job candidate_recovery_jobs%ROWTYPE; attempt candidate_recovery_attempts%ROWTYPE;
 previous candidate_recovery_samples%ROWTYPE; body JSONB; requested TIMESTAMPTZ;
 observed TIMESTAMPTZ;
BEGIN
 SELECT * INTO job FROM candidate_recovery_jobs WHERE id=sample.job_id;
 SELECT * INTO attempt FROM candidate_recovery_attempts WHERE id=sample.attempt_id;
 IF (attempt.job_id=job.id AND sample.created_at>=attempt.started_at
   AND sample.created_at<least(attempt.lease_expires_at,job.window_end)
   AND isfinite(sample.created_at)) IS NOT TRUE THEN
   RAISE EXCEPTION '恢复验证样本缺少同源尝试或固定窗口'; END IF;
 IF sample.sequence>1 THEN
   SELECT * INTO previous FROM candidate_recovery_samples
     WHERE attempt_id=sample.attempt_id AND sequence=sample.sequence-1;
   IF (previous.job_id=sample.job_id AND
     sample.created_at>=previous.created_at+interval '2 seconds')
     IS NOT TRUE THEN RAISE EXCEPTION '恢复验证样本必须连续且间隔至少2秒'; END IF;
 END IF;
 body:=sample.observation;
 IF body IS NULL THEN
   IF (char_length(sample.error_code) BETWEEN 1 AND 100
     AND sample.error_code ~ '^[a-z][a-z0-9_]*$') IS NOT TRUE THEN
     RAISE EXCEPTION '恢复验证来源失败缺少有界错误码'; END IF;
   RETURN;
 END IF;
 requested:=candidate_release_timestamp(body->'requested_at');
 observed:=candidate_release_timestamp(body->'observed_at');
 IF (body ?& ARRAY['source','path','requested_at','observed_at','http_status','value','order_id']
   AND body-ARRAY['source','path','requested_at','observed_at','http_status','value',
     'order_id']='{}'
   AND body->>'source'='local-order-probe-v1'
   AND body->>'path'='/orders/opsswarm-verification-probe'
   AND body->>'requested_at'=candidate_recovery_utc_text(requested)
   AND body->>'observed_at'=candidate_recovery_utc_text(observed)
   AND requested>=attempt.started_at AND requested>=job.window_start
   AND requested<=observed AND observed<=sample.created_at
   AND observed-requested<=interval '10 seconds'
   AND sample.created_at-observed<=interval '10 seconds'
   AND (sample.sequence=1 OR requested>=previous.created_at+interval '2 seconds')
   AND jsonb_typeof(body->'http_status')='number'
   AND body->>'http_status' ~ '^[0-9]+$'
   AND (body->>'http_status')::numeric BETWEEN 200 AND 599
   AND jsonb_typeof(body->'value')='number'
   AND (((body->>'http_status')::numeric BETWEEN 200 AND 299
     AND body->'value'='0'::jsonb AND body->>'order_id'='opsswarm-verification-probe') OR
     ((body->>'http_status')::numeric BETWEEN 500 AND 599 AND body->'value'='1'::jsonb
       AND body->'order_id'='null'::jsonb))) IS NOT TRUE THEN
   RAISE EXCEPTION '恢复验证样本形状、业务来源或时间不完整'; END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION guard_recovery_sample() RETURNS trigger AS $$
DECLARE job candidate_recovery_jobs%ROWTYPE; attempt candidate_recovery_attempts%ROWTYPE;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '恢复验证样本不可更新或删除'; END IF;
 PERFORM lock_recovery_parent(NEW.job_id);
 SELECT * INTO job FROM candidate_recovery_jobs WHERE id=NEW.job_id FOR UPDATE;
 SELECT * INTO attempt FROM candidate_recovery_attempts WHERE id=NEW.attempt_id;
 IF NEW.created_at>clock_timestamp()+interval '1 second' THEN
   RAISE EXCEPTION '恢复验证样本不能保存未来时刻'; END IF;
 IF (assert_recovery_source(job)='applied_current' AND job.status='running'
   AND attempt.status='running' AND attempt.number=job.attempt_count
   AND attempt.lease_token=job.lease_token AND attempt.worker_id=job.locked_by
   AND clock_timestamp()<least(job.locked_until,job.window_end)
   AND NEW.sequence=1+(SELECT count(*) FROM candidate_recovery_samples
     WHERE attempt_id=NEW.attempt_id)) IS NOT TRUE THEN
   RAISE EXCEPTION '迟到、旧尝试或非连续恢复验证样本不可保存'; END IF;
 PERFORM assert_recovery_sample(NEW);
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER recovery_sample_guard BEFORE INSERT OR UPDATE OR DELETE
 ON candidate_recovery_samples FOR EACH ROW EXECUTE FUNCTION guard_recovery_sample();
"""


_RESULTS = """
CREATE FUNCTION assert_recovery_result(result candidate_recovery_results) RETURNS VOID AS $$
DECLARE job candidate_recovery_jobs%ROWTYPE; attempt candidate_recovery_attempts%ROWTYPE;
 body JSONB; source TEXT; outcome TEXT; identifier TEXT; count_samples INTEGER;
 has_failure BOOLEAN; has_error BOOLEAN;
BEGIN
 SELECT * INTO job FROM candidate_recovery_jobs WHERE id=result.job_id;
 source:=assert_recovery_source(job); body:=result.content; outcome:=body->>'outcome';
 IF (body ?& ARRAY['version','execution_job_id','execution_result_sha256','operation_id',
   'outcome','attempt_id','reason_code','created_at','parent_advanced']
   AND body-ARRAY['version','execution_job_id','execution_result_sha256','operation_id',
     'outcome','attempt_id','reason_code','created_at','parent_advanced']='{}'
   AND body->>'version'='candidate-recovery-result-v1'
   AND body->>'execution_job_id'=job.id::text
   AND body->>'execution_result_sha256'=job.execution_result_sha256
   AND body->>'operation_id'=job.operation_id::text
   AND body->'parent_advanced'='false'::jsonb
   AND jsonb_typeof(body->'reason_code')='string'
   AND char_length(body->>'reason_code') BETWEEN 1 AND 100
   AND body->>'reason_code' ~ '^[a-z][a-z0-9_]*$'
   AND result.created_at=job.completed_at AND job.status='completed'
   AND body->>'created_at'=candidate_recovery_utc_text(result.created_at)
   AND candidate_release_timestamp(body->'created_at')=result.created_at
   AND result.content_sha256=encode(sha256(convert_to(
     candidate_canonical_json(body),'UTF8')),'hex')) IS NOT TRUE THEN
   RAISE EXCEPTION '恢复验证结果缺少精确任务、摘要或不可变终态'; END IF;
 IF source<>'applied_current' THEN
   IF (job.attempt_count=0 AND body->'attempt_id'='null'::jsonb AND
     outcome=CASE source WHEN 'unknown' THEN 'execution_unknown'
       WHEN 'applied_superseded' THEN 'not_recovered' ELSE 'not_executed' END AND
     body->>'reason_code'=CASE source WHEN 'unknown' THEN 'execution_unknown'
       WHEN 'applied_superseded' THEN 'execution_superseded' ELSE 'execution_not_applied' END)
     IS NOT TRUE THEN
     RAISE EXCEPTION '未适用执行不能采样或声称恢复'; END IF;
   RETURN;
 END IF;
 IF job.attempt_count=0 THEN
   IF (body->'attempt_id'='null'::jsonb AND outcome='source_failed'
     AND body->>'reason_code'='recovery_exhausted'
     AND result.created_at>=job.window_end) IS NOT TRUE THEN
     RAISE EXCEPTION '无尝试恢复验证只能在窗口耗尽后保守收敛'; END IF;
   RETURN;
 END IF;
 identifier:=candidate_condition_uuid(body->>'attempt_id');
 SELECT * INTO attempt FROM candidate_recovery_attempts WHERE id=identifier::uuid;
 IF (attempt.job_id=job.id AND body->>'attempt_id'=attempt.id::text
   AND attempt.number=job.attempt_count
   AND attempt.finished_at=result.created_at) IS NOT TRUE THEN
   RAISE EXCEPTION '恢复验证结果必须绑定最后同源终态尝试'; END IF;
 SELECT count(*),coalesce(bool_or(error_code IS NOT NULL),FALSE),
   coalesce(bool_or((observation->>'http_status')::integer>=500),FALSE)
   INTO count_samples,has_error,has_failure FROM candidate_recovery_samples
   WHERE attempt_id=attempt.id;
 IF attempt.status='lease_expired' THEN
   IF (outcome='source_failed' AND body->>'reason_code'='recovery_exhausted'
     AND (result.created_at>=job.window_end OR
     (attempt.number=3 AND result.created_at>=attempt.lease_expires_at))) IS NOT TRUE THEN
     RAISE EXCEPTION '耗尽恢复验证只能保存来源失败'; END IF;
 ELSIF (result.created_at<least(attempt.lease_expires_at,job.window_end)
   AND ((has_error AND outcome='source_failed' AND attempt.status='failed'
       AND body->>'reason_code'='probe_source_failed') OR
     (NOT has_error AND count_samples=3 AND
       outcome=CASE WHEN has_failure THEN 'not_recovered' ELSE 'recovered' END AND
       body->>'reason_code'=CASE WHEN has_failure THEN 'business_probe_failed'
         ELSE 'business_probe_passed' END AND
       attempt.status=CASE WHEN has_failure THEN 'failed' ELSE 'succeeded' END))) IS NOT TRUE THEN
   RAISE EXCEPTION '恢复验证结论与同一尝试完整样本不一致'; END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION guard_recovery_result() RETURNS trigger AS $$
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '恢复验证结果不可更新或删除'; END IF;
 PERFORM lock_recovery_parent(NEW.job_id);
 PERFORM 1 FROM candidate_recovery_jobs WHERE id=NEW.job_id FOR UPDATE;
 IF NEW.created_at>clock_timestamp()+interval '1 second' THEN
   RAISE EXCEPTION '恢复验证结果不能保存未来时刻'; END IF;
 PERFORM assert_recovery_result(NEW);
 RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER recovery_result_guard BEFORE INSERT OR UPDATE OR DELETE
 ON candidate_recovery_results FOR EACH ROW EXECUTE FUNCTION guard_recovery_result();
"""


_STATE = """
CREATE FUNCTION assert_recovery_history(job candidate_recovery_jobs) RETURNS VOID AS $$
DECLARE attempt candidate_recovery_attempts%ROWTYPE; result candidate_recovery_results%ROWTYPE;
 sample candidate_recovery_samples%ROWTYPE; item candidate_recovery_attempts%ROWTYPE;
 previous candidate_recovery_attempts%ROWTYPE;
BEGIN
 PERFORM assert_recovery_source(job);
 IF (SELECT count(*) FROM candidate_recovery_attempts WHERE job_id=job.id)<>job.attempt_count
   OR EXISTS (SELECT 1 FROM candidate_recovery_attempts WHERE job_id=job.id AND
     (number>job.attempt_count OR (number<job.attempt_count AND status<>'lease_expired'))) THEN
   RAISE EXCEPTION '恢复验证连续尝试历史不完整'; END IF;
 FOR item IN SELECT * FROM candidate_recovery_attempts WHERE job_id=job.id LOOP
   IF (isfinite(item.started_at) AND item.started_at>=job.created_at
     AND item.started_at>=job.window_start AND item.started_at<job.window_end
     AND item.lease_expires_at=item.started_at+interval '30 seconds'
     AND char_length(item.worker_id) BETWEEN 1 AND 100
     AND (item.status<>'lease_expired' OR
       item.finished_at>=least(item.lease_expires_at,job.window_end))) IS NOT TRUE THEN
     RAISE EXCEPTION '恢复验证尝试租约、窗口或过期历史不完整'; END IF;
   IF item.number>1 THEN
     SELECT * INTO previous FROM candidate_recovery_attempts
       WHERE job_id=job.id AND number=item.number-1;
     IF (previous.status='lease_expired' AND previous.finished_at=item.started_at
       AND previous.lease_expires_at<=item.started_at) IS NOT TRUE THEN
       RAISE EXCEPTION '恢复验证前驱尝试与重新领取时刻不一致'; END IF;
   END IF;
   IF (SELECT count(*) FROM candidate_recovery_samples WHERE attempt_id=item.id)<>
      coalesce((SELECT max(sequence) FROM candidate_recovery_samples WHERE attempt_id=item.id),0)
      THEN RAISE EXCEPTION '恢复验证连续样本历史不完整'; END IF;
 END LOOP;
 FOR sample IN SELECT * FROM candidate_recovery_samples WHERE job_id=job.id LOOP
   PERFORM assert_recovery_sample(sample);
 END LOOP;
 SELECT * INTO attempt FROM candidate_recovery_attempts
   WHERE job_id=job.id AND number=job.attempt_count;
 SELECT * INTO result FROM candidate_recovery_results WHERE job_id=job.id;
 IF job.status='queued' THEN
   IF job.attempt_count<>0 OR result.job_id IS NOT NULL THEN
     RAISE EXCEPTION '恢复验证queued历史不完整'; END IF;
 ELSIF job.status='running' THEN
   IF (attempt.status='running' AND attempt.lease_token=job.lease_token
     AND attempt.worker_id=job.locked_by AND attempt.lease_expires_at=job.locked_until
     AND result.job_id IS NULL) IS NOT TRUE THEN
     RAISE EXCEPTION '恢复验证当前租约历史不完整'; END IF;
 ELSE
   IF result.job_id IS NULL THEN RAISE EXCEPTION '恢复验证缺少同事务终态结果'; END IF;
   PERFORM assert_recovery_result(result);
 END IF;
END;
$$ LANGUAGE plpgsql;

CREATE FUNCTION validate_recovery_state() RETURNS trigger AS $$
DECLARE job candidate_recovery_jobs%ROWTYPE; attempt candidate_recovery_attempts%ROWTYPE;
 result candidate_recovery_results%ROWTYPE; job_tx XID; attempt_tx XID; result_tx XID;
BEGIN
 IF TG_TABLE_NAME='candidate_recovery_jobs' THEN
   SELECT * INTO job FROM candidate_recovery_jobs WHERE id=NEW.id;
 ELSE SELECT * INTO job FROM candidate_recovery_jobs WHERE id=NEW.job_id; END IF;
 PERFORM assert_recovery_history(job);
 IF TG_TABLE_NAME='candidate_recovery_samples' THEN
   SELECT * INTO attempt FROM candidate_recovery_attempts WHERE id=NEW.attempt_id;
   IF clock_timestamp()>=least(attempt.lease_expires_at,job.window_end) THEN
     RAISE EXCEPTION '恢复验证样本事务已越过租约或固定窗口截止'; END IF;
 END IF;
 IF job.status='running' THEN
   IF TG_TABLE_NAME='candidate_recovery_jobs' OR TG_TABLE_NAME='candidate_recovery_attempts' THEN
     IF clock_timestamp()>=least(job.locked_until,job.window_end) THEN
       RAISE EXCEPTION '恢复验证领取事务已越过租约或固定窗口截止'; END IF;
   END IF;
 ELSIF job.status='completed' THEN
   SELECT * INTO result FROM candidate_recovery_results WHERE job_id=job.id;
   SELECT xmin INTO job_tx FROM candidate_recovery_jobs WHERE id=job.id;
   SELECT xmin INTO result_tx FROM candidate_recovery_results WHERE job_id=job.id;
   IF job_tx IS DISTINCT FROM result_tx THEN
     RAISE EXCEPTION '恢复验证任务与结果必须同事务结算'; END IF;
   IF job.attempt_count>0 THEN
     SELECT * INTO attempt FROM candidate_recovery_attempts
       WHERE job_id=job.id AND number=job.attempt_count;
     SELECT xmin INTO attempt_tx FROM candidate_recovery_attempts WHERE id=attempt.id;
     IF job_tx IS DISTINCT FROM attempt_tx THEN
       RAISE EXCEPTION '恢复验证尝试与结果必须同事务结算'; END IF;
     IF attempt.status<>'lease_expired' AND
       clock_timestamp()>=least(attempt.lease_expires_at,job.window_end) THEN
       RAISE EXCEPTION '恢复验证终态事务已越过租约或固定窗口截止'; END IF;
   END IF;
 END IF;
 RETURN NULL;
END;
$$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER recovery_job_state AFTER INSERT OR UPDATE ON candidate_recovery_jobs
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_recovery_state();
CREATE CONSTRAINT TRIGGER recovery_attempt_state AFTER INSERT OR UPDATE ON
 candidate_recovery_attempts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION validate_recovery_state();
CREATE CONSTRAINT TRIGGER recovery_sample_state AFTER INSERT ON candidate_recovery_samples
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_recovery_state();
CREATE CONSTRAINT TRIGGER recovery_result_state AFTER INSERT ON candidate_recovery_results
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_recovery_state();
"""


def downgrade() -> None:
    op.execute("""
      DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM candidate_recovery_jobs)
          OR EXISTS (SELECT 1 FROM candidate_recovery_attempts)
          OR EXISTS (SELECT 1 FROM candidate_recovery_samples)
          OR EXISTS (SELECT 1 FROM candidate_recovery_results) THEN
          RAISE EXCEPTION '不能删除已保存的候选恢复验证事实'; END IF;
      END $$;
      DROP FUNCTION validate_recovery_state() CASCADE;
      DROP FUNCTION assert_recovery_history(candidate_recovery_jobs);
      DROP FUNCTION guard_recovery_result() CASCADE;
      DROP FUNCTION assert_recovery_result(candidate_recovery_results);
      DROP FUNCTION guard_recovery_sample() CASCADE;
      DROP FUNCTION assert_recovery_sample(candidate_recovery_samples);
      DROP FUNCTION guard_recovery_attempt() CASCADE;
      DROP FUNCTION guard_recovery_job() CASCADE;
      DROP FUNCTION lock_recovery_parent(UUID);
      DROP FUNCTION assert_recovery_source(candidate_recovery_jobs);
      DROP FUNCTION candidate_recovery_utc_text(TIMESTAMPTZ);
      DROP TABLE candidate_recovery_results;
      DROP TABLE candidate_recovery_samples;
      DROP TABLE candidate_recovery_attempts;
      DROP TABLE candidate_recovery_jobs;
    """)
