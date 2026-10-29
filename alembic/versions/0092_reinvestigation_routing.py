"""保存精确独立审查的确定性、未分派后续意图。"""

from alembic import op

revision = "0092_reflow_routing"
down_revision = "0091_reflow_review"
branch_labels = None
depends_on = None

# 冻结SQL独立编译及保护新事实，不改变旧阶段恢复函数。
# ruff: noqa: E501

_TABLE = """
CREATE TABLE investigation_reflow_routes (
 investigation_run_id UUID PRIMARY KEY REFERENCES investigation_reflow_review_results(job_id) ON DELETE RESTRICT,
 route VARCHAR(24) NOT NULL,
 content JSONB NOT NULL,
 content_sha256 VARCHAR(64) NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 CONSTRAINT ck_reflow_routes_route CHECK (route IN ('follow_up','await_human','candidate_evaluation')),
 CONSTRAINT ck_reflow_routes_content CHECK (jsonb_typeof(content)='object' AND octet_length(content::text)<=1048576 AND content_sha256 ~ '^[0-9a-f]{64}$')
);
"""

_SOURCE = """
CREATE FUNCTION reflow_routing_source(run_id UUID,created TIMESTAMPTZ) RETURNS JSONB AS $$
DECLARE job investigation_reflow_review_jobs%ROWTYPE; result investigation_reflow_review_results%ROWTYPE;
 attempt investigation_reflow_review_attempts%ROWTYPE; owner investigation_reflow_review_attempts%ROWTYPE;
 call investigation_call_reservations%ROWTYPE; body JSONB;
BEGIN
 SELECT * INTO job FROM investigation_reflow_review_jobs WHERE id=run_id;
 IF job.status IS DISTINCT FROM 'succeeded' THEN RETURN NULL; END IF;
 PERFORM assert_reflow_review_source(job,FALSE);
 SELECT * INTO result FROM investigation_reflow_review_results WHERE job_id=job.id;
 SELECT * INTO attempt FROM investigation_reflow_review_attempts WHERE id=result.attempt_id;
 SELECT * INTO call FROM investigation_call_reservations WHERE id=result.source_call_id;
 SELECT * INTO owner FROM investigation_reflow_review_attempts WHERE id=call.reflow_review_attempt_id;
 IF (result.job_id=job.id AND result.investigation_run_id=run_id
   AND attempt.job_id=job.id AND attempt.investigation_run_id=run_id AND attempt.status='succeeded'
   AND attempt.number=job.attempt_count AND attempt.finished_at=result.created_at AND job.completed_at=result.created_at
   AND result.created_at>=attempt.started_at AND result.created_at<attempt.lease_expires_at AND result.created_at<=created
   AND (SELECT count(*) FROM investigation_reflow_review_attempts WHERE job_id=job.id)=job.attempt_count
   AND NOT EXISTS(SELECT 1 FROM investigation_reflow_review_attempts WHERE job_id=job.id AND number<job.attempt_count AND status IN ('running','succeeded'))
   AND call.investigation_run_id=run_id AND call.operation='review_reinvestigation' AND call.kind='model' AND call.status='succeeded'
   AND call.investigation_attempt_id::text=job.source->>'parent_attempt_id'
   AND owner.job_id=job.id AND owner.investigation_run_id=run_id AND owner.started_at>=job.created_at
   AND call.reserved_at>=owner.started_at AND call.finished_at>=call.reserved_at AND call.finished_at<owner.lease_expires_at AND call.finished_at<=result.created_at
   AND call.idempotency_key='reflow-review:'||owner.id::text AND call.invocation_index=0
   AND call.task_id IS NULL AND call.task_attempt_id IS NULL AND call.reflow_task_id IS NULL AND call.reflow_task_attempt_id IS NULL
   AND call.reflow_synthesis_attempt_id IS NULL AND call.reinvestigation_attempt_id IS NULL AND call.human_review_attempt_id IS NULL AND call.candidate_attempt_id IS NULL
   AND call.model_input=job.packet->'model_input' AND call.model_allocation=job.packet->'allocation' AND call.context_manifest=job.packet->'manifest'
   AND call.tool_request IS NULL AND call.call_result-ARRAY['kind','version','proposal']='{}'::jsonb
   AND call.call_result->>'version'='reinvestigation-review-result-v1' AND call.call_result->>'kind'='model') IS NOT TRUE THEN
   RAISE EXCEPTION '路由缺少真实同根审查输入、调用、成功结果和尝试闭包'; END IF;
 IF call.model_usage IS NOT NULL AND ((call.model_usage->>'input_tokens')::bigint>(call.model_allocation->>'estimated_input_tokens')::bigint
   OR (call.model_usage->>'output_tokens')::bigint>(call.model_allocation->>'output_token_limit')::bigint) THEN
   RAISE EXCEPTION '超原分配的审查结果不能进入路由'; END IF;
 PERFORM assert_reflow_review_proposal(call.call_result->'proposal',job.source);
 body:=jsonb_build_object('investigation_run_id',run_id::text,'job_id',job.id::text,'source_call_id',call.id::text,
   'source_sha256',job.source_sha256,'proposal',call.call_result->'proposal','status','recorded','synthesis_result_sha256',job.source->'synthesis_result_sha256',
   'created_at',candidate_recovery_utc_text(result.created_at));
 IF result.content IS DISTINCT FROM body OR result.content_sha256 IS DISTINCT FROM reflow_hash(body) THEN
   RAISE EXCEPTION '路由审查正文及摘要无法重建'; END IF;
 IF (owner.number<=attempt.number AND owner.status IN ('succeeded','failed','lease_expired')
   AND owner.finished_at>=call.finished_at AND owner.finished_at<=result.created_at
   AND call.error_code IS NULL) IS NOT TRUE THEN
   RAISE EXCEPTION '路由成功调用者与发布尝试不闭合'; END IF;
 RETURN job.source;
END; $$ LANGUAGE plpgsql;
CREATE FUNCTION reflow_routing_historical_basis(run_id UUID,cursor_at BIGINT,created TIMESTAMPTZ) RETURNS TEXT AS $$
DECLARE reason TEXT; run investigation_runs%ROWTYPE;
BEGIN
 reason:=reflow_execution_historical_basis(run_id,cursor_at);
 IF reason IS NOT NULL THEN RETURN reason; END IF;
 SELECT * INTO run FROM investigation_runs WHERE id=run_id;
 IF created>=run.started_at+make_interval(secs=>(run.call_budget->>'max_duration_seconds')::integer)
 THEN RETURN 'run_deadline_exceeded'; END IF;
 RETURN NULL;
END; $$ LANGUAGE plpgsql;
"""

_COMPILER = """
CREATE FUNCTION reflow_routing_base(source JSONB,review JSONB) RETURNS JSONB AS $$
DECLARE hypothesis JSONB:=source#>'{synthesis_result,proposal}'; policy JSONB:=source->'coordination_policy';
 round_at INTEGER:=(source->>'next_round_number')::integer;
 total INTEGER:=(source->>'existing_task_count')::integer+jsonb_array_length(source->'professionals');
 max_rounds INTEGER:=(policy->>'max_rounds')::integer;
 max_tasks INTEGER:=(policy->>'max_tasks_per_round')::integer;
 max_total INTEGER:=(policy->>'max_total_tasks')::integer;
 reasons JSONB; suggestions JSONB:=review->'follow_up_suggestions'; count_at INTEGER:=jsonb_array_length(suggestions);
 route TEXT; sufficient BOOLEAN:=(review->>'support_is_sufficient')::boolean; objection BOOLEAN;
BEGIN
 IF round_at NOT BETWEEN 1 AND max_rounds OR total NOT BETWEEN 0 AND max_total THEN
 RAISE EXCEPTION '路由原轮次或任务数超出冻结上限'; END IF;
 reasons:=jsonb_build_array(CASE WHEN sufficient THEN 'support_sufficient' ELSE 'support_insufficient' END);
 IF jsonb_array_length(hypothesis->'counter_evidence_ids')>0 THEN reasons:=reasons||'"hypothesis_counter_evidence_present"'::jsonb; END IF;
 IF jsonb_array_length(review->'counter_evidence_ids')>0 THEN reasons:=reasons||'"review_counter_evidence_present"'::jsonb; END IF;
 IF jsonb_array_length(review->'evidence_gaps')>0 THEN reasons:=reasons||'"evidence_gaps_present"'::jsonb; END IF;
 IF jsonb_array_length(review->'competing_explanations')>0 THEN reasons:=reasons||'"competing_explanations_present"'::jsonb; END IF;
 objection:=jsonb_array_length(reasons)>1;
 IF sufficient THEN
   IF NOT objection THEN route:='candidate_evaluation'; reasons:=reasons||'"candidate_requirements_satisfied"'::jsonb;
   ELSE route:='await_human'; reasons:=reasons||'"follow_up_suggestions_missing"'::jsonb; END IF;
 ELSE
   reasons:=reasons||jsonb_build_array(CASE WHEN count_at>0 THEN 'follow_up_suggestions_present' ELSE 'follow_up_suggestions_missing' END);
   IF count_at>0 AND max_rounds-round_at<1 THEN reasons:=reasons||'"round_limit_reached"'::jsonb; END IF;
   IF count_at>max_tasks THEN reasons:=reasons||'"per_round_task_limit_exceeded"'::jsonb; END IF;
   IF count_at>max_total-total THEN reasons:=reasons||'"total_task_limit_exceeded"'::jsonb; END IF;
   IF count_at>0 AND max_rounds>round_at AND count_at<=max_tasks AND count_at<=max_total-total
   THEN route:='follow_up'; reasons:=reasons||'"follow_up_capacity_available"'::jsonb;
   ELSE route:='await_human'; END IF;
 END IF;
 RETURN jsonb_build_object('version','review-routing-v1','status','recorded','rule_version','deterministic-review-routing-v1',
   'route',route,'reason_codes',reasons,'round_number',round_at,'existing_task_count',total,
   'max_rounds',max_rounds,'max_tasks_per_round',max_tasks,'max_total_tasks',max_total,
   'remaining_rounds',max_rounds-round_at,'remaining_task_slots',max_total-total,
   'suggested_task_count',count_at,'follow_up_suggestions',suggestions);
END; $$ LANGUAGE plpgsql;

CREATE FUNCTION reflow_routing_content(run_id UUID,created TIMESTAMPTZ,cursor_at BIGINT) RETURNS JSONB AS $$
DECLARE source JSONB; result investigation_reflow_review_results%ROWTYPE;
 job investigation_reflow_review_jobs%ROWTYPE;
 base JSONB; budget JSONB; policy JSONB; model JSONB; usage JSONB;
 reason TEXT; blockers JSONB:='[]'; constraints JSONB:='[]'; windows JSONB:='[]';
 event JSONB; item JSONB; professional JSONB; models INTEGER; tools INTEGER; idx INTEGER; cost NUMERIC;
BEGIN
 source:=reflow_routing_source(run_id,created);
 SELECT * INTO job FROM investigation_reflow_review_jobs WHERE id=run_id;
 SELECT * INTO result FROM investigation_reflow_review_results WHERE job_id=run_id;
 IF source IS NULL OR NOT isfinite(created) THEN RAISE EXCEPTION '路由缺少成功审查来源或有效时刻'; END IF;
 base:=reflow_routing_base(source,result.content->'proposal');
 budget:=reflow_review_budget_snapshot(run_id,created);
 IF EXISTS(SELECT 1 FROM jsonb_array_elements(budget->'calls') c WHERE c->>'status'='reserved')
 THEN RAISE EXCEPTION '在途调用不能冻结成不可变路由前缀'; END IF;
 reason:=reflow_routing_historical_basis(run_id,cursor_at,created);
 IF reason IS NOT NULL THEN blockers:=blockers||jsonb_build_array(reason); END IF;
 FOR event IN SELECT value FROM jsonb_array_elements((SELECT m#>'{content,events}' FROM jsonb_array_elements(source->'materials') m WHERE m->>'kind'='human_inputs')) LOOP
   IF event#>>'{command,action}'='supplement' THEN
     idx:=0;
     FOR item IN SELECT value FROM jsonb_array_elements(event#>'{command,items}') LOOP
       IF item->>'kind'='constraint' THEN constraints:=constraints||jsonb_build_array(jsonb_build_object('event_id',event->>'id','item_index',idx,'text',item->>'text')); END IF;
       idx:=idx+1;
     END LOOP;
   END IF;
 END LOOP;
 IF jsonb_array_length(constraints)>0 THEN blockers:=blockers||'"human_constraints_unresolved"'::jsonb; END IF;
 IF created>=(budget->>'deadline')::timestamptz AND NOT blockers ? 'run_deadline_exceeded'
 THEN blockers:=blockers||'"run_deadline_exceeded"'::jsonb; END IF;
 models:=CASE base->>'route' WHEN 'follow_up' THEN jsonb_array_length(base->'follow_up_suggestions')+2 WHEN 'candidate_evaluation' THEN 1 ELSE 0 END;
 tools:=CASE WHEN base->>'route'='follow_up' THEN jsonb_array_length(base->'follow_up_suggestions') ELSE 0 END;
 policy:=budget->'policy';model:=budget->'model_budget';usage:=budget->'model_usage';
 IF (budget->>'reserved_model_calls')::integer+models>(policy->>'max_model_calls')::integer
 THEN blockers:=blockers||'"model_call_budget_exceeded"'::jsonb; END IF;
 IF (budget->>'reserved_tool_calls')::integer+tools>(policy->>'max_tool_calls')::integer OR EXISTS(
   SELECT 1 FROM jsonb_array_elements(budget->'calls') c WHERE c->>'kind'='tool'
   GROUP BY coalesce(c->>'task_id',c->>'reflow_task_id') HAVING count(*)>(policy->>'max_tool_calls_per_task')::integer)
 THEN blockers:=blockers||'"tool_call_budget_exceeded"'::jsonb; END IF;
 IF (usage->>'committed_input_tokens')::bigint+models*(model->>'max_input_tokens_per_call')::bigint>(model->>'max_input_tokens')::bigint
 THEN blockers:=blockers||'"run_input_budget_exceeded"'::jsonb; END IF;
 IF (usage->>'committed_output_tokens')::bigint+models*(model->>'max_output_tokens_per_call')::bigint>(model->>'max_output_tokens')::bigint
 THEN blockers:=blockers||'"run_output_budget_exceeded"'::jsonb; END IF;
 cost:=reinvestigation_model_cost(model,(model->>'max_input_tokens_per_call')::bigint,(model->>'max_output_tokens_per_call')::bigint);
 IF (usage->>'committed_cost_usd')::numeric+models*cost>(model->>'max_estimated_cost_usd')::numeric
 THEN blockers:=blockers||'"model_cost_budget_exceeded"'::jsonb; END IF;
 IF EXISTS(SELECT 1 FROM jsonb_array_elements(budget->'calls') c WHERE c->>'kind'='model' AND c->'model_usage'<>'null'::jsonb
   AND ((c#>>'{model_usage,input_tokens}')::bigint>(c#>>'{model_allocation,estimated_input_tokens}')::bigint
   OR (c#>>'{model_usage,output_tokens}')::bigint>(c#>>'{model_allocation,output_token_limit}')::bigint))
 THEN blockers:=blockers||'"model_usage_exceeded_allocation"'::jsonb; END IF;
 FOR professional IN SELECT value FROM jsonb_array_elements(source->'professionals') LOOP
   windows:=windows||jsonb_build_array(jsonb_build_object('task_id',professional->>'task_id','effective_window',professional->'effective_window'));
 END LOOP;
 RETURN jsonb_build_object('version','reinvestigation-routing-v1','status','recorded','dispatch_status','not_dispatched',
   'investigation_run_id',run_id::text,'review_source_sha256',job.source_sha256,'review_packet_sha256',job.packet_sha256,
   'review_result_sha256',result.content_sha256,'synthesis_result_sha256',source->'synthesis_result_sha256',
   'source_call_id',result.source_call_id::text,'basis_event_cursor',cursor_at,'basis_blocker',reason,
   'base_decision',base,'route',CASE WHEN jsonb_array_length(blockers)>0 THEN 'await_human' ELSE base->>'route' END,
   'blocking_reason_codes',blockers,'unresolved_constraints',constraints,
   'unresolved_questions',source#>'{synthesis_result,proposal,unresolved_questions}','observation_windows',windows,
   'minimum_model_calls',models,'minimum_tool_calls',tools,'budget',budget,'created_at',candidate_recovery_utc_text(created));
END; $$ LANGUAGE plpgsql;
"""

_GUARDS = """
CREATE FUNCTION guard_reflow_route() RETURNS trigger AS $$
DECLARE expected JSONB; cursor_at BIGINT; reason TEXT;
BEGIN
 IF TG_OP<>'INSERT' THEN RAISE EXCEPTION '回流路由事实不可更新或删除'; END IF;
 PERFORM 1 FROM work_items WHERE investigation_run_id=NEW.investigation_run_id FOR UPDATE;
 PERFORM 1 FROM investigation_reflow_review_jobs WHERE id=NEW.investigation_run_id FOR UPDATE;
 PERFORM lock_reflow_review_basis(NEW.investigation_run_id);
 SELECT max(e.id) INTO cursor_at FROM incident_events e JOIN investigation_runs r ON r.incident_id=e.incident_id WHERE r.id=NEW.investigation_run_id;
 expected:=reflow_routing_content(NEW.investigation_run_id,NEW.created_at,cursor_at);
 reason:=reflow_review_basis_blocker(NEW.investigation_run_id,NEW.created_at);
 IF (NEW.content=expected AND NEW.content_sha256=reflow_hash(expected) AND NEW.route=expected->>'route'
   AND NEW.content->>'basis_blocker' IS NOT DISTINCT FROM reason
   AND NEW.created_at<=clock_timestamp()+interval '1 second'
   AND jsonb_array_length(expected#>'{budget,calls}')=(SELECT count(*) FROM investigation_call_reservations WHERE investigation_run_id=NEW.investigation_run_id)) IS NOT TRUE THEN
 RAISE EXCEPTION '回流路由必须绑定完整成功来源、当前游标和原账本独立编译'; END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER reflow_routes_guard BEFORE INSERT OR UPDATE OR DELETE ON investigation_reflow_routes FOR EACH ROW EXECUTE FUNCTION guard_reflow_route();

CREATE FUNCTION validate_reflow_route_commit() RETURNS trigger AS $$
DECLARE row investigation_reflow_routes%ROWTYPE; reason TEXT; cursor_at BIGINT;
BEGIN
 SELECT * INTO row FROM investigation_reflow_routes WHERE investigation_run_id=NEW.investigation_run_id;
 IF row.route<>'await_human' THEN
   reason:=reflow_review_basis_blocker(row.investigation_run_id,clock_timestamp());
   SELECT max(e.id) INTO cursor_at FROM incident_events e JOIN investigation_runs r ON r.incident_id=e.incident_id WHERE r.id=row.investigation_run_id;
   IF reason IS NOT NULL OR cursor_at IS DISTINCT FROM (row.content->>'basis_event_cursor')::bigint
     OR row.content IS DISTINCT FROM reflow_routing_content(row.investigation_run_id,row.created_at,cursor_at)
     OR jsonb_array_length(row.content#>'{budget,calls}')<>(SELECT count(*) FROM investigation_call_reservations WHERE investigation_run_id=row.investigation_run_id)
   THEN RAISE EXCEPTION '回流自动意图提交时依据、原总截止或账本已失效'; END IF;
 END IF;
 RETURN NULL;
END; $$ LANGUAGE plpgsql;
CREATE CONSTRAINT TRIGGER reflow_routes_commit AFTER INSERT ON investigation_reflow_routes DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION validate_reflow_route_commit();
"""


def upgrade() -> None:
    op.execute(_TABLE)
    op.execute(_SOURCE)
    op.execute(_COMPILER)
    op.execute(_GUARDS)


def downgrade() -> None:
    op.execute("""
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM investigation_reflow_routes) THEN RAISE EXCEPTION '不能删除已保存回流路由'; END IF;
END $$;
DROP TABLE investigation_reflow_routes;
DROP FUNCTION validate_reflow_route_commit();
DROP FUNCTION guard_reflow_route();
DROP FUNCTION reflow_routing_content(UUID,TIMESTAMPTZ,BIGINT);
DROP FUNCTION reflow_routing_base(JSONB,JSONB);
DROP FUNCTION reflow_routing_historical_basis(UUID,BIGINT,TIMESTAMPTZ);
DROP FUNCTION reflow_routing_source(UUID,TIMESTAMPTZ);
""")
