"""真实业务库与 checkpoint 库验证 v3 首轮协调图恢复。"""

import json
from datetime import datetime, timedelta
from typing import TypedDict

import httpx2
import pytest
from langgraph.graph import END, START, StateGraph

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.coordination.contracts import (
    CoordinationPlanRecord,
    CoordinationPolicy,
    CoordinationProposal,
    ProposedTask,
)
from opsswarm.investigation.coordination.planner import CoordinationPlanner
from opsswarm.investigation.coordination.queries import CoordinationPlanQueries
from opsswarm.investigation.dynamic_graph import DynamicCoordinationGraph
from opsswarm.investigation.models import InvestigationRunStatus, WorkItemError
from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO, FrozenScenarioDataSource
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.dynamic_graph import DynamicCoordinationGraphFactory
from opsswarm.worker.runner import ProcessorContext
from tests.checkpoint_fakes import ConclusionSynthesizerStub, SpecialistAnalystStub
from tests.loop_helpers import loop_policy
from tests.model_budget_helpers import sdk_model, sdk_response
from tests.task_facts_helpers import BASE_TIME, TaskClock, claimed_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def proposal() -> CoordinationProposal:
    return CoordinationProposal(
        round_number=1,
        goal="核对错误日志和最近变更",
        tasks=(
            ProposedTask(
                task_key="log",
                role="log",
                goal="检查异常日志模式",
                lookback_seconds=600,
            ),
            ProposedTask(
                task_key="change",
                role="change",
                goal="核对近期发布",
                lookback_seconds=3600,
            ),
        ),
    )


def frozen_source(request) -> FrozenScenarioDataSource:
    scenario = RELEASE_500_SCENARIO.model_copy(
        update={
            "request": request,
            "evidence": tuple(
                item.model_copy(update={"observed_at": BASE_TIME})
                for item in RELEASE_500_SCENARIO.evidence
            ),
        }
    )
    return FrozenScenarioDataSource(scenario)


def processor_context(claim) -> ProcessorContext:
    return ProcessorContext(
        request=claim.request,
        mode=claim.run.mode,
        data_source_mode=claim.run.data_source_mode,
        work_item_kind=claim.work_item.kind,
        graph_thread_id=claim.run.graph_thread_id,
        workflow_version=claim.run.workflow_version,
        graph_version=claim.run.graph_version,
        coordination_policy=claim.run.coordination_policy,
        investigation_run_id=claim.run.id,
        lease_token=claim.work_item.lease_token,
        call_budget=claim.run.call_budget,
        model_budget=claim.run.model_budget,
        context_policy_version=claim.run.context_policy_version,
        evidence_policy_version=claim.run.evidence_policy_version,
        observation_cutoff_at=claim.run.observation_cutoff_at,
    )


async def v3_claim(database_session_factory):
    return await claimed_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )


async def test_v3_first_coordination_persists_plan_and_checkpoint(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await v3_claim(database_session_factory)
    source = frozen_source(claim.request)
    requests = []

    def reply(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return sdk_response(payload, proposal().model_dump(mode="json"))

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = DynamicCoordinationGraphFactory(
            session_factory=database_session_factory,
            data_source=source,
            model=sdk_model(client),
            checkpointer=saver,
            clock=TaskClock(),
        )(processor_context(claim))
        assert claim.run.graph_thread_id is not None
        plan = await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        checkpoint = await saver.aget_tuple(
            {"configurable": {"thread_id": claim.run.graph_thread_id}}
        )
        with pytest.raises(RuntimeError, match="必须通过 resume"):
            await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        assert await graph.resume(thread_id=claim.run.graph_thread_id) == plan

    assert len(requests) == 1
    assert checkpoint is not None
    assert checkpoint.metadata["workflow_version"] == "v3"
    assert checkpoint.metadata["graph_version"] == "dynamic-investigation-graph-v1"
    assert checkpoint.checkpoint["channel_values"]["workflow_version"] == "v3"
    assert (
        checkpoint.checkpoint["channel_values"]["graph_version"] == "dynamic-investigation-graph-v1"
    )
    assert plan.plan.proposal == proposal()
    assert plan.source_call_id is not None
    plans = await CoordinationPlanQueries(database_session_factory).list_plans(claim.run.id)
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    run = await store.get_run(claim.run.id)
    assert plans == (plan,)
    assert len(budget.calls) == 1
    assert budget.calls[0].operation is CallOperation.COORDINATE
    assert budget.calls[0].status == "succeeded"
    manifest = await CallBudgetQueries(database_session_factory).get_context_manifest(
        claim.run.id, plan.source_call_id
    )
    assert manifest is not None and manifest.workflow_version == "v3"
    assert run.status is InvestigationRunStatus.RUNNING and run.result is None


class ProposalThenCrash:
    def __init__(self, planner: CoordinationPlanner) -> None:
        self.planner = planner

    async def create_first_plan(
        self,
        *,
        policy: CoordinationPolicy,
        observation_cutoff_at: datetime,
        evidence_snapshot_id: str,
    ) -> tuple[CoordinationPlanRecord, bool, bool]:
        await self.planner.propose_first_round(
            policy=policy,
            observation_cutoff_at=observation_cutoff_at,
            evidence_snapshot_id=evidence_snapshot_id,
        )
        raise RuntimeError("模拟模型成功后计划提交前崩溃")


class PlanThenCrash:
    def __init__(self, planner: CoordinationPlanner) -> None:
        self.planner = planner
        self.plan: CoordinationPlanRecord | None = None

    async def create_first_plan(
        self,
        *,
        policy: CoordinationPolicy,
        observation_cutoff_at: datetime,
        evidence_snapshot_id: str,
    ) -> tuple[CoordinationPlanRecord, bool, bool]:
        result = await self.planner.create_first_plan(
            policy=policy,
            observation_cutoff_at=observation_cutoff_at,
            evidence_snapshot_id=evidence_snapshot_id,
        )
        self.plan = result[0]
        raise RuntimeError("模拟计划提交后 checkpoint 前崩溃")


async def failed_then_reclaimed(store, claim, clock):
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="协调节点崩溃"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="replacement-v3-worker",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    return replacement


@pytest.mark.parametrize("window", ["after_model", "after_plan"])
async def test_v3_resume_reuses_committed_business_fact_across_crash_window(
    database_session_factory,
    checkpoint_database_url,
    window,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await v3_claim(database_session_factory)
    source = frozen_source(claim.request)
    clock = TaskClock()
    policy = claim.run.coordination_policy
    assert policy is not None and claim.work_item.lease_token is not None
    calls = []

    def reply(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return sdk_response(payload, proposal().model_dump(mode="json"))

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client:
        original = CoordinationPlanner(
            database_session_factory,
            run_id=claim.run.id,
            lease_token=claim.work_item.lease_token,
            model=sdk_model(client),
            clock=clock,
        )
        crashing = (
            ProposalThenCrash(original) if window == "after_model" else PlanThenCrash(original)
        )
        async with open_checkpoint_saver(checkpoint_database_url) as saver:
            graph = DynamicCoordinationGraph(
                run_id=claim.run.id,
                planner=crashing,
                policy=policy,
                observation_cutoff_at=source.snapshot_time,
                evidence_snapshot_id=source.snapshot_id,
                checkpointer=saver,
            )
            assert claim.run.graph_thread_id is not None
            with pytest.raises(RuntimeError, match="模拟"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
            assert await graph.has_checkpoint(thread_id=claim.run.graph_thread_id)
            checkpoint = await saver.aget_tuple(
                {"configurable": {"thread_id": claim.run.graph_thread_id}}
            )
            assert checkpoint is not None
            assert str(claim.work_item.lease_token) not in str(checkpoint.checkpoint)

    before = await CoordinationPlanQueries(database_session_factory).list_plans(claim.run.id)
    assert (len(before) == 0) if window == "after_model" else (len(before) == 1)
    replacement = await failed_then_reclaimed(store, claim, clock)
    assert replacement.work_item.lease_token is not None

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: pytest.fail("恢复不应再次调用模型"))
    ) as client:
        recovered_planner = CoordinationPlanner(
            database_session_factory,
            run_id=claim.run.id,
            lease_token=replacement.work_item.lease_token,
            model=sdk_model(client),
            clock=clock,
        )
        async with open_checkpoint_saver(checkpoint_database_url) as saver:
            recovered_graph = DynamicCoordinationGraph(
                run_id=claim.run.id,
                planner=recovered_planner,
                policy=policy,
                observation_cutoff_at=source.snapshot_time,
                evidence_snapshot_id=source.snapshot_id,
                checkpointer=saver,
            )
            recovered = await recovered_graph.resume(thread_id=replacement.run.graph_thread_id)

    plans = await CoordinationPlanQueries(database_session_factory).list_plans(claim.run.id)
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert plans == (recovered,)
    assert len(calls) == 1
    assert len(budget.calls) == 1 and budget.calls[0].status == "succeeded"


class CheckpointContractState(TypedDict):
    workflow_version: str
    graph_version: str


@pytest.mark.parametrize("conflict", ["workflow", "graph"])
async def test_v3_refuses_incompatible_checkpoint_before_coordination(
    checkpoint_database_url,
    conflict,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    from uuid import uuid4

    run_id = uuid4()
    thread_id = f"investigation-run:{run_id}"

    async with open_checkpoint_saver(checkpoint_database_url) as saver:
        if conflict == "workflow":
            legacy = LangGraphParallelInvestigator(
                data_source=FrozenScenarioDataSource(RELEASE_500_SCENARIO),
                analyst=SpecialistAnalystStub(),
                synthesizer=ConclusionSynthesizerStub(),
                checkpointer=saver,
            )
            await legacy.investigate(RELEASE_500_SCENARIO.request, thread_id=thread_id)
        else:
            builder = StateGraph(CheckpointContractState)
            builder.add_node("finish", lambda state: state)
            builder.add_edge(START, "finish")
            builder.add_edge("finish", END)
            incompatible = builder.compile(checkpointer=saver)
            await incompatible.ainvoke(
                {"workflow_version": "v3", "graph_version": "obsolete-graph-v1"},
                config={
                    "metadata": {
                        "workflow_version": "v3",
                        "graph_version": "obsolete-graph-v1",
                    },
                    "configurable": {"thread_id": thread_id},
                },
            )

        graph = DynamicCoordinationGraph(
            run_id=run_id,
            planner=pytest.fail,
            policy=CoordinationPolicy(),
            observation_cutoff_at=BASE_TIME,
            evidence_snapshot_id="unused-snapshot-v1",
            checkpointer=saver,
        )
        message = "工作流版本" if conflict == "workflow" else "图版本"
        with pytest.raises(RuntimeError, match=message):
            await graph.resume(thread_id=thread_id)
