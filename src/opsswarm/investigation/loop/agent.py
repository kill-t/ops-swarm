"""装配框架原生专业工具循环，恢复边界仍由外层持久任务管理。"""

import json
from collections.abc import Sequence

from langchain.agents import create_agent
from langchain.agents.middleware.types import InputAgentState
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy
from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.loop.contracts import LoopModelInput
from opsswarm.investigation.loop.messages import to_langchain
from opsswarm.investigation.loop.middleware import EvidenceLoopMiddleware, UsageCapture
from opsswarm.investigation.loop.policy import conflict
from opsswarm.investigation.loop.service import EvidenceLoopService
from opsswarm.investigation.loop.tools import evidence_tool, runbook_tool
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationRequest,
    SpecialistReport,
)


class EvidenceLoopAnalyst:
    def __init__(
        self,
        model: BaseChatModel,
        *,
        policy: ModelBudgetPolicy,
        runner: BudgetedCallRunner,
        service: EvidenceLoopService,
    ) -> None:
        self._model, self._policy, self._runner, self._service = model, policy, runner, service

    async def analyze(
        self,
        *,
        specialist: EvidenceKind,
        request: InvestigationRequest,
        evidence: Sequence[EvidenceObservation],
    ) -> SpecialistReport:
        capture = UsageCapture()
        middleware = EvidenceLoopMiddleware(
            runner=self._runner,
            service=self._service,
            policy=self._policy,
            role=specialist.value,
            capture=capture,
        )
        await self._runner.check()
        packet = await self._service.prepare(
            middleware.task_id,
            middleware.attempt_id,
            CallOperation(f"analyze_{specialist.value}"),
            0,
            self._policy,
        )
        inputs = packet.model_input
        assert isinstance(inputs, LoopModelInput)
        payload = json.loads(inputs.messages[1].content)
        if (
            payload["incident"] != request.model_dump(mode="json")
            or {e.evidence_id for e in evidence}
            != {e["evidence_id"] for e in payload["evidence_catalog"]["entries"]}
            or any(e.kind != specialist for e in evidence)
        ):
            raise conflict("图输入与专业持久目录不一致")
        tools = [evidence_tool(middleware.read_evidence)]
        if "read_runbook" in packet.manifest.tool_names:
            tools.append(runbook_tool(middleware.read_runbook))
        graph = create_agent(
            model=self._model,
            tools=tools,
            response_format=ToolStrategy(SpecialistReport, handle_errors=False),
            middleware=[middleware],
            # 内部循环不接入外层 checkpoint；技术重试使用新的任务尝试。
            checkpointer=False,
        )
        initial: InputAgentState = {"messages": list(to_langchain(inputs.messages))}
        result = await graph.ainvoke(
            initial,
            config={"callbacks": [capture], "recursion_limit": 32},
        )
        return SpecialistReport.model_validate(result["structured_response"])
