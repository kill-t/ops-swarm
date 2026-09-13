"""v3 动态调查图的首轮协调节点与 checkpoint 契约。"""

from collections.abc import Awaitable
from datetime import datetime
from typing import NotRequired, Protocol, TypedDict, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple
from langgraph.graph import END, START, StateGraph

from opsswarm.investigation.coordination.contracts import (
    CoordinationPlanRecord,
    CoordinationPolicy,
)
from opsswarm.investigation.graph_thread import build_graph_thread_id, normalize_graph_thread_id
from opsswarm.investigation.models import InvestigationRequest
from opsswarm.investigation.workflow_versions import (
    DYNAMIC_INVESTIGATION_GRAPH_VERSION,
    InvestigationGraphVersion,
    WorkflowVersion,
)


class FirstRoundPlanner(Protocol):
    """动态图协调节点依赖的最小规划端口。"""

    def create_first_plan(
        self,
        *,
        policy: CoordinationPolicy,
        observation_cutoff_at: datetime,
        evidence_snapshot_id: str,
    ) -> Awaitable[tuple[CoordinationPlanRecord, bool, bool]]:
        """创建或复用首轮计划。"""


class _DynamicGraphState(TypedDict):
    request: InvestigationRequest
    workflow_version: str
    graph_version: str
    coordination_plan: NotRequired[CoordinationPlanRecord]


class _DynamicGraphUpdate(TypedDict, total=False):
    coordination_plan: CoordinationPlanRecord


class DynamicCoordinationGraph:
    """只推进 v3 首轮协调，后续节点完成前不产生调查终态。"""

    def __init__(
        self,
        *,
        run_id: UUID,
        planner: FirstRoundPlanner,
        policy: CoordinationPolicy,
        observation_cutoff_at: datetime,
        evidence_snapshot_id: str,
        checkpointer: BaseCheckpointSaver[str],
        graph_version: InvestigationGraphVersion = DYNAMIC_INVESTIGATION_GRAPH_VERSION,
    ) -> None:
        if graph_version != DYNAMIC_INVESTIGATION_GRAPH_VERSION:
            raise ValueError("动态图版本不受支持")
        self._run_id = run_id
        self._planner = planner
        self._policy = policy
        self._observation_cutoff_at = observation_cutoff_at
        self._evidence_snapshot_id = evidence_snapshot_id
        self._checkpointer = checkpointer
        self._graph_version = graph_version
        builder = StateGraph(_DynamicGraphState)
        builder.add_node("coordinate", self._coordinate)
        builder.add_edge(START, "coordinate")
        builder.add_edge("coordinate", END)
        self._workflow = builder.compile(
            checkpointer=checkpointer,
            name="dynamic_investigation_v1",
        )

    async def investigate(
        self,
        request: InvestigationRequest,
        *,
        thread_id: str,
    ) -> CoordinationPlanRecord:
        """首次执行首轮协调；已有状态的线程必须走恢复入口。"""

        if await self.has_checkpoint(thread_id=thread_id):
            raise RuntimeError("已有 checkpoint 的 v3 线程必须通过 resume 恢复")
        state = await self._workflow.ainvoke(
            {
                "request": request,
                "workflow_version": WorkflowVersion.V3.value,
                "graph_version": self._graph_version,
            },
            config=self._execution_config(thread_id),
        )
        return await self._validated_plan(cast(_DynamicGraphState, state))

    async def resume(self, *, thread_id: str) -> CoordinationPlanRecord:
        """从同一运行的兼容 checkpoint 恢复首轮协调。"""

        if not await self.has_checkpoint(thread_id=thread_id):
            raise RuntimeError("恢复线程没有 checkpoint")
        state = await self._workflow.ainvoke(None, config=self._execution_config(thread_id))
        return await self._validated_plan(cast(_DynamicGraphState, state))

    async def has_checkpoint(self, *, thread_id: str) -> bool:
        """读取 checkpoint，并同时核对 metadata 与状态中的双版本。"""

        checkpoint = await self._checkpointer.aget_tuple(self._execution_config(thread_id))
        if checkpoint is not None:
            self._validate_checkpoint_contract(checkpoint)
        return checkpoint is not None

    def draw_mermaid(self) -> str:
        return self._workflow.get_graph().draw_mermaid()

    async def _coordinate(self, state: _DynamicGraphState) -> _DynamicGraphUpdate:
        if (
            state.get("workflow_version") != WorkflowVersion.V3.value
            or state.get("graph_version") != self._graph_version
        ):
            raise RuntimeError("动态图状态版本不兼容")
        plan, _, _ = await self._planner.create_first_plan(
            policy=self._policy,
            observation_cutoff_at=self._observation_cutoff_at,
            evidence_snapshot_id=self._evidence_snapshot_id,
        )
        if plan.investigation_run_id != self._run_id:
            raise RuntimeError("协调计划不属于当前调查运行")
        return {"coordination_plan": plan}

    async def _validated_plan(self, state: _DynamicGraphState) -> CoordinationPlanRecord:
        saved_plan = state.get("coordination_plan")
        if saved_plan is None:
            raise RuntimeError("动态图首轮协调没有返回计划")
        try:
            plan = CoordinationPlanRecord.model_validate(saved_plan)
        except ValueError as error:
            raise RuntimeError("动态图首轮协调计划无法通过契约校验") from error
        if (
            state.get("workflow_version") != WorkflowVersion.V3.value
            or state.get("graph_version") != self._graph_version
            or plan.investigation_run_id != self._run_id
        ):
            raise RuntimeError("动态图结果版本或运行身份不兼容")
        persisted, _, _ = await self._planner.create_first_plan(
            policy=self._policy,
            observation_cutoff_at=self._observation_cutoff_at,
            evidence_snapshot_id=self._evidence_snapshot_id,
        )
        if persisted != plan:
            raise RuntimeError("checkpoint 协调计划与业务事实不一致")
        return persisted

    def _execution_config(self, thread_id: str) -> RunnableConfig:
        expected = build_graph_thread_id(self._run_id)
        normalized = normalize_graph_thread_id(thread_id)
        if normalized != expected:
            raise ValueError("v3 线程必须匹配绑定的调查运行")
        return {
            "max_concurrency": 1,
            "metadata": {
                "workflow_version": WorkflowVersion.V3.value,
                "graph_version": self._graph_version,
            },
            "configurable": {"thread_id": normalized},
        }

    def _validate_checkpoint_contract(self, checkpoint: CheckpointTuple) -> None:
        values = checkpoint.checkpoint["channel_values"]
        initial = values.get("__start__")
        state = initial if isinstance(initial, dict) else values
        if checkpoint.metadata.get("workflow_version") != WorkflowVersion.V3.value:
            raise RuntimeError("checkpoint 工作流版本不兼容")
        if state.get("workflow_version") != WorkflowVersion.V3.value:
            raise RuntimeError("checkpoint 状态工作流版本不兼容")
        if checkpoint.metadata.get("graph_version") != self._graph_version:
            raise RuntimeError("checkpoint 图版本不兼容")
        if state.get("graph_version") != self._graph_version:
            raise RuntimeError("checkpoint 状态图版本不兼容")
