"""v3 图 v6 的首轮调查、确定性路由、第二轮计划与恢复。"""

import math
import operator
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, NotRequired, TypedDict, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from opsswarm.investigation.async_source import (
    AsyncInvestigationDataSource,
    ThreadedInvestigationDataSource,
)
from opsswarm.investigation.coordination.contracts import CoordinationPlanRecord, CoordinationPolicy
from opsswarm.investigation.dynamic_contracts import (
    FirstRoundInvestigationResult,
    FirstRoundReviewResult,
    FirstRoundRoutingResult,
    FirstRoundSynthesisResult,
    FollowUpPlanningResult,
)
from opsswarm.investigation.dynamic_graph import FirstRoundPlanner
from opsswarm.investigation.errors import SpecialistKindMismatch, UnverifiedEvidenceReference
from opsswarm.investigation.follow_up_planning.service import FollowUpPlanService
from opsswarm.investigation.graph_thread import build_graph_thread_id, normalize_graph_thread_id
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation, InvestigationRequest
from opsswarm.investigation.parallel_graph import SpecialistAnalyst
from opsswarm.investigation.review.contracts import HypothesisReviewRecord
from opsswarm.investigation.review.service import IndependentHypothesisReviewer
from opsswarm.investigation.review_routing.contracts import ReviewRoutingRecord
from opsswarm.investigation.review_routing.service import ReviewRoutingService
from opsswarm.investigation.scenarios import FrozenScenarioDataSource
from opsswarm.investigation.synthesis.contracts import OpenHypothesisRecord
from opsswarm.investigation.synthesis.service import FirstRoundSynthesizer
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.results import SpecialistTaskResult
from opsswarm.investigation.tasks.runtime import DynamicTaskRuntime
from opsswarm.investigation.workflow_versions import (
    LEGACY_DYNAMIC_FOLLOW_UP_GRAPH_VERSION,
    WorkflowVersion,
)


class _DynamicFollowUpState(TypedDict):
    request: InvestigationRequest
    workflow_version: str
    graph_version: str
    specialist_results: Annotated[list[SpecialistTaskResult], operator.add]
    coordination_plan: NotRequired[CoordinationPlanRecord]
    open_hypothesis: NotRequired[OpenHypothesisRecord]
    hypothesis_review: NotRequired[HypothesisReviewRecord]
    review_route: NotRequired[ReviewRoutingRecord]
    follow_up_plan: NotRequired[CoordinationPlanRecord | None]


class _DynamicFollowUpUpdate(TypedDict, total=False):
    coordination_plan: CoordinationPlanRecord
    specialist_results: list[SpecialistTaskResult]
    open_hypothesis: OpenHypothesisRecord
    hypothesis_review: HypothesisReviewRecord
    review_route: ReviewRoutingRecord
    follow_up_plan: CoordinationPlanRecord | None


class _SpecialistInput(TypedDict):
    request: InvestigationRequest
    workflow_version: str
    graph_version: str
    round_id: UUID
    assignment: TaskAssignment


class DynamicFollowUpGraph:
    """执行动态首轮并按补查路由创建第二轮计划，仍不执行该轮任务。"""

    def __init__(
        self,
        *,
        run_id: UUID,
        planner: FirstRoundPlanner,
        policy: CoordinationPolicy,
        observation_cutoff_at: datetime,
        evidence_snapshot_id: str,
        data_source: FrozenScenarioDataSource,
        analyst: SpecialistAnalyst,
        task_runtime: DynamicTaskRuntime,
        synthesizer: FirstRoundSynthesizer,
        reviewer: IndependentHypothesisReviewer,
        router: ReviewRoutingService,
        follow_up_planner: FollowUpPlanService,
        checkpointer: BaseCheckpointSaver[str],
    ) -> None:
        if (
            task_runtime.run_id != run_id
            or task_runtime.snapshot_id != evidence_snapshot_id
            or task_runtime.data_source_mode != "frozen"
            or data_source.snapshot_id != evidence_snapshot_id
            or data_source.snapshot_time != observation_cutoff_at
        ):
            raise ValueError("动态首轮图的运行、来源与任务快照不一致")
        self._run_id = run_id
        self._planner = planner
        self._policy = policy
        self._observation_cutoff_at = observation_cutoff_at
        self._evidence_snapshot_id = evidence_snapshot_id
        self._data_source: AsyncInvestigationDataSource = ThreadedInvestigationDataSource(
            data_source
        )
        self._analyst = analyst
        self._runtime = task_runtime
        self._synthesizer = synthesizer
        self._reviewer = reviewer
        self._router = router
        self._follow_up_planner = follow_up_planner
        self._checkpointer = checkpointer
        builder = StateGraph(_DynamicFollowUpState)
        builder.add_node("coordinate", self._coordinate)
        builder.add_node("investigate_specialist", self._investigate_specialist)
        builder.add_node("synthesize_open_hypothesis", self._synthesize_open_hypothesis)
        builder.add_node("review_open_hypothesis", self._review_open_hypothesis)
        builder.add_node("determine_review_route", self._determine_review_route)
        builder.add_node("compile_follow_up_plan", self._compile_follow_up_plan)
        builder.add_edge(START, "coordinate")
        builder.add_conditional_edges("coordinate", self._dispatch)
        builder.add_edge("investigate_specialist", "synthesize_open_hypothesis")
        builder.add_edge("synthesize_open_hypothesis", "review_open_hypothesis")
        builder.add_edge("review_open_hypothesis", "determine_review_route")
        builder.add_conditional_edges(
            "determine_review_route",
            self._route_after_review,
            {"follow_up": "compile_follow_up_plan", "complete": END},
        )
        builder.add_edge("compile_follow_up_plan", END)
        self._workflow = builder.compile(
            checkpointer=checkpointer,
            name="dynamic_investigation_v6",
        )

    async def investigate(
        self, request: InvestigationRequest, *, thread_id: str
    ) -> FollowUpPlanningResult:
        if await self.has_checkpoint(thread_id=thread_id):
            raise RuntimeError("已有 checkpoint 的 v3 线程必须通过 resume 恢复")
        state = await self._workflow.ainvoke(
            {
                "request": request,
                "workflow_version": WorkflowVersion.V3.value,
                "graph_version": LEGACY_DYNAMIC_FOLLOW_UP_GRAPH_VERSION,
                "specialist_results": [],
            },
            config=self._execution_config(thread_id),
        )
        return await self._validated_result(cast(_DynamicFollowUpState, state))

    async def resume(self, *, thread_id: str) -> FollowUpPlanningResult:
        if not await self.has_checkpoint(thread_id=thread_id):
            raise RuntimeError("恢复线程没有 checkpoint")
        state = await self._workflow.ainvoke(None, config=self._execution_config(thread_id))
        return await self._validated_result(cast(_DynamicFollowUpState, state))

    async def has_checkpoint(self, *, thread_id: str) -> bool:
        checkpoint = await self._checkpointer.aget_tuple(self._execution_config(thread_id))
        if checkpoint is not None:
            self._validate_checkpoint_contract(checkpoint)
        return checkpoint is not None

    def draw_mermaid(self) -> str:
        return self._workflow.get_graph().draw_mermaid()

    async def _coordinate(self, state: _DynamicFollowUpState) -> _DynamicFollowUpUpdate:
        self._validate_state_version(state)
        plan, _, _ = await self._planner.create_first_plan(
            policy=self._policy,
            observation_cutoff_at=self._observation_cutoff_at,
            evidence_snapshot_id=self._evidence_snapshot_id,
        )
        if plan.investigation_run_id != self._run_id:
            raise RuntimeError("协调计划不属于当前调查运行")
        return {"coordination_plan": plan}

    def _dispatch(self, state: _DynamicFollowUpState) -> list[Send]:
        self._validate_state_version(state)
        plan = CoordinationPlanRecord.model_validate(state.get("coordination_plan"))
        return [
            Send(
                "investigate_specialist",
                _SpecialistInput(
                    request=state["request"],
                    workflow_version=state["workflow_version"],
                    graph_version=state["graph_version"],
                    round_id=plan.round_id,
                    assignment=assignment,
                ),
            )
            for assignment in sorted(plan.plan.assignment.tasks, key=lambda item: item.task_key)
        ]

    async def _investigate_specialist(self, state: _SpecialistInput) -> _DynamicFollowUpUpdate:
        self._validate_state_version(state)
        assignment = TaskAssignment.model_validate(state["assignment"])

        async def operation() -> SpecialistTaskResult:
            evidence = tuple(await self._runtime.call_tool(assignment.role, query))
            report = await self._runtime.call_model(
                assignment.role,
                lambda: self._analyst.analyze(
                    specialist=assignment.role,
                    request=state["request"],
                    evidence=evidence,
                ),
            )
            if report.specialist is not assignment.role:
                raise SpecialistKindMismatch(
                    f"{assignment.role.value} 节点返回了 {report.specialist.value} 报告"
                )
            unknown_ids = set(report.evidence_ids) - {item.evidence_id for item in evidence}
            if unknown_ids:
                raise UnverifiedEvidenceReference(unknown_ids)
            return SpecialistTaskResult(
                evidence_snapshot_id=self._runtime.snapshot_id,
                data_source_mode=self._runtime.data_source_mode,
                evidence=evidence,
                report=report,
            )

        async def query() -> Sequence[EvidenceObservation]:
            seconds = (assignment.window_end - assignment.window_start).total_seconds()
            if assignment.role is EvidenceKind.LOG:
                items = await self._data_source.query_logs(
                    service_name=assignment.service_name,
                    lookback_minutes=math.ceil(seconds / 60),
                )
            elif assignment.role is EvidenceKind.METRIC:
                items = await self._data_source.query_metrics(
                    service_name=assignment.service_name,
                    lookback_minutes=math.ceil(seconds / 60),
                )
            else:
                items = await self._data_source.list_recent_changes(
                    service_name=assignment.service_name,
                    lookback_hours=math.ceil(seconds / 3600),
                )
            return tuple(
                item
                for item in items
                if item.kind is assignment.role
                and assignment.window_start <= item.observed_at <= assignment.window_end
            )

        result = await self._runtime.run(
            round_id=state["round_id"],
            expected_round_number=1,
            assignment=assignment,
            operation=operation,
        )
        return {"specialist_results": [result]}

    async def _synthesize_open_hypothesis(
        self, state: _DynamicFollowUpState
    ) -> _DynamicFollowUpUpdate:
        first_round = await self._validated_first_round(state)
        record = await self._synthesizer.synthesize(state["request"], first_round)
        return {"open_hypothesis": record}

    async def _review_open_hypothesis(self, state: _DynamicFollowUpState) -> _DynamicFollowUpUpdate:
        synthesis = await self._validated_synthesis(state)
        record = await self._reviewer.review(state["request"], synthesis)
        return {"hypothesis_review": record}

    async def _determine_review_route(self, state: _DynamicFollowUpState) -> _DynamicFollowUpUpdate:
        review = await self._validated_review(state)
        record = await self._router.route(review)
        return {"review_route": record}

    def _route_after_review(self, state: _DynamicFollowUpState) -> str:
        self._validate_state_version(state)
        route = ReviewRoutingRecord.model_validate(state.get("review_route"))
        return "follow_up" if route.decision.route == "follow_up" else "complete"

    async def _compile_follow_up_plan(self, state: _DynamicFollowUpState) -> _DynamicFollowUpUpdate:
        routing = await self._validated_routing(state)
        plan = await self._follow_up_planner.plan(routing)
        if plan is None:
            raise RuntimeError("补查路由没有生成第二轮协调计划")
        return {"follow_up_plan": plan}

    async def _validated_first_round(
        self, state: _DynamicFollowUpState
    ) -> FirstRoundInvestigationResult:
        self._validate_state_version(state)
        try:
            result = FirstRoundInvestigationResult(
                coordination_plan=CoordinationPlanRecord.model_validate(
                    state.get("coordination_plan")
                ),
                specialist_results=tuple(
                    sorted(
                        (
                            SpecialistTaskResult.model_validate(item)
                            for item in state["specialist_results"]
                        ),
                        key=lambda item: item.report.specialist.value,
                    )
                ),
            )
        except (KeyError, ValueError) as error:
            raise RuntimeError("动态首轮结果无法通过契约校验") from error
        persisted_plan, _, _ = await self._planner.create_first_plan(
            policy=self._policy,
            observation_cutoff_at=self._observation_cutoff_at,
            evidence_snapshot_id=self._evidence_snapshot_id,
        )
        if persisted_plan != result.coordination_plan:
            raise RuntimeError("checkpoint 协调计划与业务事实不一致")
        completed = [
            await self._runtime.get_completed(
                round_id=persisted_plan.round_id,
                expected_round_number=1,
                assignment=assignment,
            )
            for assignment in persisted_plan.plan.assignment.tasks
        ]
        persisted_results = tuple(sorted(completed, key=lambda item: item.report.specialist.value))
        if persisted_results != result.specialist_results:
            raise RuntimeError("checkpoint 专业结果与业务事实不一致")
        return result

    async def _validated_synthesis(self, state: _DynamicFollowUpState) -> FirstRoundSynthesisResult:
        first_round = await self._validated_first_round(state)
        try:
            result = FirstRoundSynthesisResult(
                **first_round.model_dump(),
                open_hypothesis=OpenHypothesisRecord.model_validate(state.get("open_hypothesis")),
            )
        except ValueError as error:
            raise RuntimeError("动态首轮综合结果无法通过契约校验") from error
        persisted = await self._synthesizer.get_completed(first_round)
        if persisted != result.open_hypothesis:
            raise RuntimeError("checkpoint 开放假设与业务事实不一致")
        return result

    async def _validated_review(self, state: _DynamicFollowUpState) -> FirstRoundReviewResult:
        synthesis = await self._validated_synthesis(state)
        try:
            result = FirstRoundReviewResult(
                **synthesis.model_dump(),
                hypothesis_review=HypothesisReviewRecord.model_validate(
                    state.get("hypothesis_review")
                ),
            )
        except ValueError as error:
            raise RuntimeError("动态首轮审查结果无法通过契约校验") from error
        persisted = await self._reviewer.get_completed(synthesis)
        if persisted != result.hypothesis_review:
            raise RuntimeError("checkpoint 独立审查与业务事实不一致")
        return result

    async def _validated_routing(self, state: _DynamicFollowUpState) -> FirstRoundRoutingResult:
        review = await self._validated_review(state)
        try:
            result = FirstRoundRoutingResult(
                **review.model_dump(),
                review_route=ReviewRoutingRecord.model_validate(state.get("review_route")),
            )
        except ValueError as error:
            raise RuntimeError("动态首轮路由结果无法通过契约校验") from error
        persisted = await self._router.get_completed(review)
        if persisted != result.review_route:
            raise RuntimeError("checkpoint 确定性路由与业务事实不一致")
        return result

    async def _validated_result(self, state: _DynamicFollowUpState) -> FollowUpPlanningResult:
        routing = await self._validated_routing(state)
        try:
            result = FollowUpPlanningResult(
                **routing.model_dump(),
                follow_up_plan=(
                    CoordinationPlanRecord.model_validate(state["follow_up_plan"])
                    if state.get("follow_up_plan") is not None
                    else None
                ),
            )
        except (KeyError, ValueError) as error:
            raise RuntimeError("补查计划结果无法通过契约校验") from error
        persisted = await self._follow_up_planner.get_completed(routing)
        if persisted != result.follow_up_plan:
            raise RuntimeError("checkpoint 补查计划与业务事实不一致")
        return result

    @staticmethod
    def _validate_state_version(state: Mapping[str, object]) -> None:
        if (
            state.get("workflow_version") != WorkflowVersion.V3.value
            or state.get("graph_version") != LEGACY_DYNAMIC_FOLLOW_UP_GRAPH_VERSION
        ):
            raise RuntimeError("动态首轮图状态版本不兼容")

    def _execution_config(self, thread_id: str) -> RunnableConfig:
        expected = build_graph_thread_id(self._run_id)
        normalized = normalize_graph_thread_id(thread_id)
        if normalized != expected:
            raise ValueError("v3 线程必须匹配绑定的调查运行")
        return {
            "max_concurrency": 3,
            "metadata": {
                "workflow_version": WorkflowVersion.V3.value,
                "graph_version": LEGACY_DYNAMIC_FOLLOW_UP_GRAPH_VERSION,
            },
            "configurable": {"thread_id": normalized},
        }

    @staticmethod
    def _validate_checkpoint_contract(checkpoint: CheckpointTuple) -> None:
        values = checkpoint.checkpoint["channel_values"]
        initial = values.get("__start__")
        state = initial if isinstance(initial, dict) else values
        if checkpoint.metadata.get("workflow_version") != WorkflowVersion.V3.value:
            raise RuntimeError("checkpoint 工作流版本不兼容")
        if state.get("workflow_version") != WorkflowVersion.V3.value:
            raise RuntimeError("checkpoint 状态工作流版本不兼容")
        if checkpoint.metadata.get("graph_version") != LEGACY_DYNAMIC_FOLLOW_UP_GRAPH_VERSION:
            raise RuntimeError("checkpoint 图版本不兼容")
        if state.get("graph_version") != LEGACY_DYNAMIC_FOLLOW_UP_GRAPH_VERSION:
            raise RuntimeError("checkpoint 状态图版本不兼容")
