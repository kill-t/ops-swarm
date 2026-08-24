import pytest

from opsswarm.investigation.models import (
    InvestigationMode,
    InvestigationRequest,
    MultiAgentInvestigationResult,
    WorkItemKind,
)
from opsswarm.worker import ProcessorContext
from opsswarm.worker.processor import LangGraphMultiAgentProcessor

pytestmark = pytest.mark.asyncio


class UnusedParallelInvestigator:
    """路由校验失败时，任何图方法都不应被调用。"""

    async def has_checkpoint(self, *, thread_id: str) -> bool:
        del thread_id
        raise AssertionError("不应查询 checkpoint")

    async def investigate(
        self,
        request: InvestigationRequest,
        *,
        thread_id: str | None = None,
    ) -> MultiAgentInvestigationResult:
        del request, thread_id
        raise AssertionError("不应首次运行图")

    async def resume(self, *, thread_id: str) -> MultiAgentInvestigationResult:
        del thread_id
        raise AssertionError("不应恢复图")


async def test_multi_agent_processor_rejects_mismatched_work_item_kind() -> None:
    processor = LangGraphMultiAgentProcessor(
        investigator=UnusedParallelInvestigator(),
        model_name="unused-model",
    )
    context = ProcessorContext(
        request=InvestigationRequest(
            incident_id="00000000-0000-0000-0000-000000000001",
            service_name="order-service",
            title="订单接口异常",
            started_at="2026-01-01T08:00:00Z",
        ),
        mode=InvestigationMode.MULTI_AGENT,
        work_item_kind=WorkItemKind.RUN_SINGLE_AGENT_INVESTIGATION,
        graph_thread_id="investigation-run:00000000-0000-0000-0000-000000000001",
    )

    with pytest.raises(RuntimeError, match="调查运行模式与持久任务类型不匹配"):
        await processor.process(context)
