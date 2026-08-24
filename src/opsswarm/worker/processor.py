"""当前单 Agent 基线的 Worker 处理器。"""

from langchain_core.language_models.chat_models import BaseChatModel

from opsswarm.investigation.agent import SingleAgentInvestigator
from opsswarm.investigation.evaluation import evaluate_investigation
from opsswarm.investigation.models import InvestigationRequest
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO, FrozenScenarioDataSource
from opsswarm.worker.runner import ProcessorOutcome


class LangChainBaselineProcessor:
    """使用冻结演练数据运行真实可注入模型；不连接生产遥测。"""

    def __init__(self, *, model: BaseChatModel, model_name: str) -> None:
        self._investigator = SingleAgentInvestigator(
            model=model,
            data_source=FrozenScenarioDataSource(RELEASE_500_SCENARIO),
        )
        self._model_name = model_name

    async def process(self, request: InvestigationRequest) -> ProcessorOutcome:
        result = await self._investigator.investigate(request)
        return ProcessorOutcome(
            result=result,
            metrics=evaluate_investigation(result, RELEASE_500_SCENARIO),
            model_name=self._model_name,
        )
