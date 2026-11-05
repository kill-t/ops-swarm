"""真实 PG/checkpoint 验证现场动态图；窗口客户端为假实现，不验收 MCP 协议。"""

import json
import traceback
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx2
import pytest

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.experience import context_store as experience_context_store
from opsswarm.experience.context_tables import ExperienceContextRow
from opsswarm.incidents import ActorType, AlertSubmission, IncidentActor, IncidentSeverity
from opsswarm.incidents.persistence import IncidentStore
from opsswarm.investigation.budget.contracts import CallBudgetPolicy, CallOperation
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.handoff.persistence import TerminalHandoffStore
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationDataSourceMode,
    InvestigationMode,
)
from opsswarm.investigation.persistence import InvestigationStore
from opsswarm.investigation.query_execution import ReadOnlyQueryExecutor
from opsswarm.investigation.review.persistence import HypothesisReviewStore
from opsswarm.investigation.review.queries import HypothesisReviewQueries
from opsswarm.investigation.review_routing.queries import ReviewRoutingQueries
from opsswarm.investigation.tasks.evidence import live_snapshot_id
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.dynamic_factory import CurrentDynamicGraphFactory
from opsswarm.worker.dynamic_processor import DynamicInvestigationProcessor
from opsswarm.worker.runner import InvestigationWorker
from tests.live_dynamic_postgres import (
    live_dynamic_postgres_config as live_dynamic_postgres_config,
)
from tests.live_dynamic_postgres import (
    migrated_database_url as migrated_database_url,
)
from tests.live_task_helpers import LiveHttpFixture
from tests.loop_helpers import loop_policy, tool_response
from tests.model_budget_helpers import sdk_model, sdk_response
from tests.task_facts_helpers import TaskClock

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CUTOFF = datetime(2025, 1, 1, 12, tzinfo=UTC)
POLICY = "task-evidence-live-dynamic-mcp-v1"
GRAPH = "dynamic-investigation-graph-v16"
LIVE = InvestigationDataSourceMode.LIVE


@pytest.fixture
def live_query_executor():
    executor = ReadOnlyQueryExecutor()
    try:
        yield executor
    finally:
        executor.close()


class WindowedTelemetry:
    """模拟 MCP 客户端端口，记录精确窗口；协议传输由既有测试独立覆盖。"""

    def __init__(self):
        self.calls = []
        self.forbidden = False

    def read(self, role, service_name, window):
        assert not self.forbidden, "恢复不得重新采集已提交的现场证据"
        self.calls.append((role, service_name, window))
        return (
            EvidenceObservation(
                evidence_id=f"dynamic-live-{role.value}",
                kind=role,
                source=("loki-fault-lab" if role is EvidenceKind.LOG else "prometheus-fault-lab"),
                summary="固定遥测响应，仅用于验证协作与恢复",
                observed_at=window.window_end - timedelta(seconds=1),
                attributes={
                    "service_name": service_name,
                    "window_start": window.window_start.isoformat(),
                    "window_end": window.window_end.isoformat(),
                },
            ),
        )

    def query_logs_in_window(self, *, service_name, window):
        return self.read(EvidenceKind.LOG, service_name, window)

    def query_metrics_in_window(self, *, service_name, window):
        return self.read(EvidenceKind.METRIC, service_name, window)


class LiveReplies:
    """首轮日志缺少指标，第二轮只补查指标，再交由人工判断。"""

    def __init__(self, db):
        self.budgets = CallBudgetQueries(db)
        self.reviews = []
        self.syntheses = []
        self.review_budgets = []
        self.request_count = 0

    async def __call__(self, request):
        self.request_count += 1
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        if "policy" in data and "round_number" in data:
            return sdk_response(
                payload,
                {
                    "round_number": 1,
                    "goal": "先调查错误日志，再根据证据缺口决定是否补查",
                    "tasks": [
                        {
                            "task_key": "log",
                            "role": "log",
                            "goal": "调查本次错误日志",
                            "lookback_seconds": 600,
                        }
                    ],
                },
            )
        if "working_summaries" not in data:
            catalog = data["evidence_catalog"]
            if len(payload["messages"]) == 2:
                return tool_response(
                    "read_evidence",
                    {
                        "evidence_id": catalog["entries"][0]["evidence_id"],
                        "snapshot_sha256": catalog["snapshot_sha256"],
                    },
                    call_id=f"read-{data['specialist']}",
                )
            detail = json.loads(payload["messages"][-1]["content"])
            return tool_response(
                "SpecialistReport",
                {
                    "specialist": data["specialist"],
                    "summary": "读取专业证据后形成调查意见",
                    "findings": ["已核对本任务授权窗口内的材料"],
                    "evidence_ids": [detail["evidence"]["evidence_id"]],
                    "confidence": 0.7,
                },
                call_id=f"report-{data['specialist']}",
            )
        if "open_hypothesis" not in data:
            self.syntheses.append(data)
            return sdk_response(
                payload,
                {
                    "statement": "遥测提示依赖异常，仍需独立审查适用范围",
                    "confidence": 0.7,
                    "supporting_evidence_ids": [data["evidence"][0]["evidence_id"]],
                    "counter_evidence_ids": [],
                    "unresolved_questions": ["是否存在尚未排除的依赖异常？"],
                },
            )
        run_id = UUID(data["task_context"]["investigation_run_id"])
        self.review_budgets.append(await self.budgets.get_snapshot(run_id))
        self.reviews.append(data)
        first = len(self.reviews) == 1
        return sdk_response(
            payload,
            {
                "support_is_sufficient": False,
                "rationale": "需要指标补查" if first else "补查后仍需工程师确认外部依赖状态",
                "accepted_supporting_evidence_ids": [],
                "counter_evidence_ids": [],
                "evidence_gaps": ["缺少依赖指标" if first else "缺少外部依赖的人工确认"],
                "competing_explanations": [],
                "suggested_direction": "follow_up" if first else "human_review",
                "follow_up_suggestions": (
                    [
                        {
                            "role": "metric",
                            "question": "补查依赖等待队列",
                            "rationale": "区分应用错误与依赖容量异常",
                        }
                    ]
                    if first
                    else []
                ),
            },
        )


async def create_live_run(db, *, experience=False):
    """直接通过创建用例启动当前现场 v3，不修改旧运行的持久版本。"""
    key = str(uuid4())
    incident = await IncidentStore(db).ingest_alert(
        AlertSubmission(
            source="live-dynamic-test",
            external_event_id=key,
            fingerprint=key,
            service_name="order-service",
            severity=IncidentSeverity.P1,
            title="现场动态协作测试",
            payload={},
            occurred_at=CUTOFF - timedelta(minutes=1),
            ingested_at=CUTOFF - timedelta(minutes=1),
        )
    )
    store = InvestigationStore(db)
    started = await store.start_investigation(
        incident.incident.id,
        expected_version=1,
        occurred_at=CUTOFF,
        actor=IncidentActor(actor_type=ActorType.HUMAN, actor_id="live-test-engineer"),
        reason="直接开始现场动态调查",
        mode=InvestigationMode.MULTI_AGENT,
        workflow_version=WorkflowVersion.V3,
        data_source_mode=LIVE,
        call_budget=CallBudgetPolicy(max_model_calls=9, max_duration_seconds=900),
        model_budget=loop_policy(),
        coordination_experience_policy_version="coordination-experience-v1" if experience else None,
    )
    assert started.run.graph_version == GRAPH
    assert started.run.evidence_policy_version == POLICY
    assert started.run.observation_cutoff_at == CUTOFF
    return store, started


def make_worker(db, store, source, client, saver, clock, query_executor):
    class RecordingProcessor(DynamicInvestigationProcessor):
        errors = []

        async def process(self, context):
            try:
                return await super().process(context)
            except Exception:
                self.errors.append(traceback.format_exc())
                raise

    processor = RecordingProcessor(
        factory=CurrentDynamicGraphFactory(
            session_factory=db,
            data_source=source,
            model=sdk_model(client),
            checkpointer=saver,
            clock=clock,
            query_executor=query_executor,
        ),
        model_name="fake-budget",
    )
    return InvestigationWorker(
        store=store,
        processor=processor,
        worker_id="live-dynamic-worker",
        clock=clock,
        data_source_mode=LIVE,
        handoff_store=TerminalHandoffStore(db),
        model_name="fake-budget",
        lease_duration=timedelta(seconds=300),
        processing_timeout=timedelta(seconds=240),
        base_retry_delay=timedelta(0),
    )


@pytest.mark.parametrize("interrupt_second_review", [False, True])
async def test_live_dynamic_follow_up_handoff_and_committed_call_recovery(
    database_session_factory,
    checkpoint_database_url,
    live_query_executor,
    monkeypatch,
    interrupt_second_review,
):
    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await create_live_run(db, experience=interrupt_second_review)
    clock = TaskClock(CUTOFF + timedelta(seconds=2))
    telemetry = WindowedTelemetry()
    fixture = LiveHttpFixture(CUTOFF, clock, mcp_logs=telemetry, mcp_metrics=telemetry)
    replies = LiveReplies(db)
    original_save = HypothesisReviewStore.save
    saves = 0

    async def interrupt_save(self, *args, **kwargs):
        nonlocal saves
        saves += 1
        if saves == 2:
            raise RuntimeError("模拟第二轮审查模型成功后、业务事实提交前中断")
        return await original_save(self, *args, **kwargs)

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        with monkeypatch.context() as patch:
            if interrupt_second_review:
                patch.setattr(HypothesisReviewStore, "save", interrupt_save)
            worker = make_worker(
                db, store, fixture.source, client, saver, clock, live_query_executor
            )
            assert await worker.run_once()
            for error in worker._processor.errors:
                if not (interrupt_second_review and "模拟第二轮审查模型成功后" in error):
                    pytest.fail(error, pytrace=False)

    budget_queries = CallBudgetQueries(db)
    before = await budget_queries.get_snapshot(started.run.id)
    if interrupt_second_review:
        assert (await store.get_run(started.run.id)).status == "queued"
        assert len(await HypothesisReviewQueries(db).list_for_run(started.run.id)) == 1
        assert before.calls[-1].operation is CallOperation.REVIEW
        assert before.calls[-1].status == "succeeded"
        assert await TerminalHandoffStore(db).get(started.run.id) is None
        facts_before = await CollaborationQueries(db).get_snapshot(started.run.id)
        telemetry.forbidden = True
        clock.current += timedelta(seconds=1)

        def forbidden_model(_):
            raise AssertionError("已提交成功模型结果恢复不得再次发送 SDK 请求")

        async def forbidden_selection(*args, **kwargs):
            raise AssertionError("恢复不得重新选择当前有效经验")

        monkeypatch.setattr(experience_context_store, "_current_selection", forbidden_selection)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(forbidden_model)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            worker = make_worker(
                db, store, fixture.source, client, saver, clock, live_query_executor
            )
            assert await worker.run_once()
            if worker._processor.errors:
                pytest.fail("\n".join(worker._processor.errors), pytrace=False)
            assert not await worker.run_once()
        assert await budget_queries.get_snapshot(started.run.id) == before
        assert await CollaborationQueries(db).get_snapshot(started.run.id) == facts_before

    run = await store.get_run(started.run.id)
    work = await store.get_work_item(started.work_item.id)
    handoff = await TerminalHandoffStore(db).get(run.id)
    assert run.status == "await_human" and run.completed_at is run.result is None, (
        worker._processor.errors
    )
    assert handoff is not None and handoff.destination == "await_human"
    assert work.status == "completed" and work.locked_by is None
    assert work.lease_token is work.locked_until is None
    attempts = await store.list_attempts(run.id)
    assert len(attempts) == (2 if interrupt_second_review else 1)
    assert attempts[-1].status == "handed_off" and not attempts[-1].will_retry
    assert handoff.investigation_attempt_id == attempts[-1].id

    facts = CollaborationQueries(db)
    snapshot = await facts.get_snapshot(run.id)
    assert [record.round_number for record in snapshot.rounds] == [1, 2]
    assert all(record.status == "completed" for record in snapshot.rounds)
    assert [[task.assignment.role for task in record.tasks] for record in snapshot.rounds] == [
        [EvidenceKind.LOG],
        [EvidenceKind.METRIC],
    ]
    assert not fixture.requests, "现场图不能旁路 MCP 端口或读取未分配的变更来源"
    assert len(telemetry.calls) == 2
    for record, (role, service, window) in zip(snapshot.rounds, telemetry.calls, strict=True):
        task = record.tasks[0]
        assignment = task.assignment
        evidence = await facts.get_evidence(run.id, task.id)
        summary = await facts.get_working_summary(run.id, task.id)
        result = await facts.get_result(run.id, task.id)
        assert task.status == "succeeded" and task.attempt_count == 1
        assert evidence is not None and summary is not None and result is not None
        assert evidence.snapshot.version == POLICY and evidence.snapshot.data_source_mode == "live"
        assert evidence.snapshot.evidence_snapshot_id == live_snapshot_id(run.id, CUTOFF)
        assert evidence.snapshot.evidence_snapshot_id == assignment.evidence_snapshot_id
        assert evidence.snapshot.role is assignment.role is role
        assert evidence.snapshot.service_name == assignment.service_name == service
        assert evidence.snapshot.window_start == assignment.window_start == window.window_start
        assert evidence.snapshot.window_end == assignment.window_end == window.window_end == CUTOFF
        assert CUTOFF - assignment.window_start == timedelta(
            minutes=10 if role is EvidenceKind.LOG else 15
        )
        assert summary.attempt_id == result.attempt_id == evidence.attempt_id

    routes = await ReviewRoutingQueries(db).list_for_run(run.id)
    reviews = await HypothesisReviewQueries(db).list_for_run(run.id)
    assert [route.decision.route for route in routes] == ["follow_up", "await_human"]
    assert len(reviews) == len(replies.reviews) == len(replies.syntheses) == 2
    assert handoff.source_review_route_id == routes[-1].id
    assert handoff.source_sha256 == routes[-1].content_sha256
    budget = await budget_queries.get_snapshot(run.id)
    if interrupt_second_review:
        async with db() as session:
            selection = await session.get(ExperienceContextRow, run.id)
            assert selection is not None
            assert selection.first_call_id == budget.calls[0].id
            assert selection.content["scope"]["data_source_mode"] == "live"
            assert selection.content["items"] == []
    assert budget.reserved_model_calls == 9 and budget.reserved_tool_calls == 4
    assert all(call.status == "succeeded" for call in budget.calls)
    first, second = replies.review_budgets
    assert first.started_at == second.started_at == budget.started_at
    assert first.deadline == second.deadline == budget.deadline
    assert first.reserved_model_calls == 5 and second.reserved_model_calls == 9
    assert [call.id for call in first.calls] == [
        call.id for call in budget.calls[: len(first.calls)]
    ]
    assert budget.model_usage is not None and budget.model_usage.unknown_model_calls == 0
    assert budget.model_usage.reported_input_tokens == 9 * 80
    assert budget.model_usage.reported_output_tokens == 9 * 20
    for call in budget.calls:
        if call.kind == "model":
            manifest = await budget_queries.get_context_manifest(run.id, call.id)
            assert manifest is not None and manifest.evidence_policy_version == POLICY
            assert manifest.workflow_version == "v3"
            if call.operation is CallOperation.REVIEW:
                assert manifest.role == "reviewer" and not manifest.tool_names
    events = await facts.list_events(run.id)
    assert [event.event_type for event in events].count("run.handed_off") == 1
    assert events[-1].event_type == "run.handed_off"
    async with open_checkpoint_saver(checkpoint_database_url) as saver:
        checkpoint = await saver.aget_tuple({"configurable": {"thread_id": run.graph_thread_id}})
        assert checkpoint is not None and checkpoint.metadata["graph_version"] == GRAPH
