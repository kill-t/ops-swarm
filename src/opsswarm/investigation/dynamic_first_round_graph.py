"""v3 图 v2 的首轮协调、动态专业分派与恢复。"""

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
from opsswarm.investigation.dynamic_contracts import FirstRoundInvestigationResult
from opsswarm.investigation.dynamic_graph import FirstRoundPlanner
from opsswarm.investigation.errors import SpecialistKindMismatch, UnverifiedEvidenceReference
from opsswarm.investigation.graph_thread import build_graph_thread_id, normalize_graph_thread_id
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation, InvestigationRequest
from opsswarm.investigation.parallel_graph import SpecialistAnalyst
from opsswarm.investigation.scenarios import FrozenScenarioDataSource
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.results import SpecialistTaskResult
from opsswarm.investigation.tasks.runtime import DynamicTaskRuntime
from opsswarm.investigation.workflow_versions import (
    LEGACY_DYNAMIC_FIRST_ROUND_GRAPH_VERSION,
    WorkflowVersion,
)


class _DynamicFirstRoundState(TypedDict):
    request: InvestigationRequest
    workflow_version: str
    graph_version: str
    specialist_results: Annotated[list[SpecialistTaskResult], operator.add]
    coordination_plan: NotRequired[CoordinationPlanRecord]


class _DynamicFirstRoundUpdate(TypedDict, total=False):
    coordination_plan: CoordinationPlanRecord
    specialist_results: list[SpecialistTaskResult]


class _SpecialistInput(TypedDict):
    request: InvestigationRequest
    workflow_version: str
    graph_version: str
    round_id: UUID
    assignment: TaskAssignment


class DynamicFirstRoundGraph:
    """执行首轮计划选择的专业任务，完成后仍保持父调查非终态。"""

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
        self._checkpointer = checkpointer
        builder = StateGraph(_DynamicFirstRoundState)
        builder.add_node("coordinate", self._coordinate)
        builder.add_node("investigate_specialist", self._investigate_specialist)
        builder.add_edge(START, "coordinate")
        builder.add_conditional_edges("coordinate", self._dispatch)
        builder.add_edge("investigate_specialist", END)
        self._workflow = builder.compile(
            checkpointer=checkpointer,
            name="dynamic_investigation_v2",
        )

    async def investigate(
        self, request: InvestigationRequest, *, thread_id: str
    ) -> FirstRoundInvestigationResult:
        if await self.has_checkpoint(thread_id=thread_id):
            raise RuntimeError("已有 checkpoint 的 v3 线程必须通过 resume 恢复")
        state = await self._workflow.ainvoke(
            {
                "request": request,
                "workflow_version": WorkflowVersion.V3.value,
                "graph_version": LEGACY_DYNAMIC_FIRST_ROUND_GRAPH_VERSION,
                "specialist_results": [],
            },
            config=self._execution_config(thread_id),
        )
        return await self._validated_result(cast(_DynamicFirstRoundState, state))

    async def resume(self, *, thread_id: str) -> FirstRoundInvestigationResult:
        if not await self.has_checkpoint(thread_id=thread_id):
            raise RuntimeError("恢复线程没有 checkpoint")
        state = await self._workflow.ainvoke(None, config=self._execution_config(thread_id))
        return await self._validated_result(cast(_DynamicFirstRoundState, state))

    async def has_checkpoint(self, *, thread_id: str) -> bool:
        checkpoint = await self._checkpointer.aget_tuple(self._execution_config(thread_id))
        if checkpoint is not None:
            self._validate_checkpoint_contract(checkpoint)
        return checkpoint is not None

    def draw_mermaid(self) -> str:
        return self._workflow.get_graph().draw_mermaid()

    async def _coordinate(self, state: _DynamicFirstRoundState) -> _DynamicFirstRoundUpdate:
        self._validate_state_version(state)
        plan, _, _ = await self._planner.create_first_plan(
            policy=self._policy,
            observation_cutoff_at=self._observation_cutoff_at,
            evidence_snapshot_id=self._evidence_snapshot_id,
        )
        if plan.investigation_run_id != self._run_id:
            raise RuntimeError("协调计划不属于当前调查运行")
        return {"coordination_plan": plan}

    def _dispatch(self, state: _DynamicFirstRoundState) -> list[Send]:
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

    async def _investigate_specialist(self, state: _SpecialistInput) -> _DynamicFirstRoundUpdate:
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
            assignment=assignment,
            operation=operation,
        )
        return {"specialist_results": [result]}

    async def _validated_result(
        self, state: _DynamicFirstRoundState
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
                round_id=persisted_plan.round_id, assignment=assignment
            )
            for assignment in persisted_plan.plan.assignment.tasks
        ]
        persisted_results = tuple(sorted(completed, key=lambda item: item.report.specialist.value))
        if persisted_results != result.specialist_results:
            raise RuntimeError("checkpoint 专业结果与业务事实不一致")
        return result

    @staticmethod
    def _validate_state_version(state: Mapping[str, object]) -> None:
        if (
            state.get("workflow_version") != WorkflowVersion.V3.value
            or state.get("graph_version") != LEGACY_DYNAMIC_FIRST_ROUND_GRAPH_VERSION
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
                "graph_version": LEGACY_DYNAMIC_FIRST_ROUND_GRAPH_VERSION,
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
        if checkpoint.metadata.get("graph_version") != LEGACY_DYNAMIC_FIRST_ROUND_GRAPH_VERSION:
            raise RuntimeError("checkpoint 图版本不兼容")
        if state.get("graph_version") != LEGACY_DYNAMIC_FIRST_ROUND_GRAPH_VERSION:
            raise RuntimeError("checkpoint 状态图版本不兼容")
