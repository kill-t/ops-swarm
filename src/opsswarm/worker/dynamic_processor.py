"""当前动态图的版本分派、checkpoint 恢复与终端引用转换。"""

from collections.abc import Callable

from opsswarm.investigation.dynamic_third_round_routing_graph import DynamicThirdRoundRoutingGraph
from opsswarm.investigation.models import (
    InvestigationMode,
    WorkItemKind,
)
from opsswarm.investigation.workflow_versions import (
    CURRENT_DYNAMIC_GRAPH_VERSIONS,
    WorkflowVersion,
    is_supported_dynamic_source,
)
from opsswarm.worker.runner import (
    InvestigationProcessor,
    ProcessorContext,
    ProcessorOutcome,
    TerminalProcessorOutcome,
)


class DynamicInvestigationProcessor:
    """历史 v3 图不自动升级；返回后由业务事务验证并释放父租约。"""

    def __init__(
        self,
        *,
        factory: Callable[[ProcessorContext], DynamicThirdRoundRoutingGraph],
        model_name: str,
    ) -> None:
        self._factory = factory
        self._model_name = model_name

    async def process(self, context: ProcessorContext) -> TerminalProcessorOutcome:
        if (
            context.workflow_version is not WorkflowVersion.V3
            or context.graph_version not in CURRENT_DYNAMIC_GRAPH_VERSIONS
            or context.mode is not InvestigationMode.MULTI_AGENT
            or not is_supported_dynamic_source(
                context.graph_version,
                context.data_source_mode.value,
                context.evidence_policy_version,
            )
            or context.work_item_kind is not WorkItemKind.RUN_MULTI_AGENT_INVESTIGATION
            or context.graph_thread_id is None
        ):
            raise ValueError("动态处理器只接受来源匹配的当前图及v3多Agent调查")
        graph = self._factory(context)
        if await graph.has_checkpoint(thread_id=context.graph_thread_id):
            result = await graph.resume(thread_id=context.graph_thread_id)
        else:
            result = await graph.investigate(context.request, thread_id=context.graph_thread_id)
        route = (
            result.third_round_review_route or result.follow_up_review_route or result.review_route
        )
        if route.decision.route == "follow_up":
            raise ValueError("动态图尚未到达终端，不能交接中间补查路由")
        return TerminalProcessorOutcome(
            source_review_route_id=route.id,
            source_sha256=route.content_sha256,
            model_name=self._model_name,
        )


class WorkflowRoutingProcessor:
    """在已存在的模式分派之外选择独立 v3 结果契约。"""

    def __init__(
        self, *, legacy: InvestigationProcessor, dynamic: DynamicInvestigationProcessor
    ) -> None:
        self._legacy = legacy
        self._dynamic = dynamic

    async def process(
        self, context: ProcessorContext
    ) -> ProcessorOutcome | TerminalProcessorOutcome:
        processor: InvestigationProcessor = (
            self._dynamic if context.workflow_version is WorkflowVersion.V3 else self._legacy
        )
        return await processor.process(context)
