"""受预算约束的真实模型单场景评测运行器。"""

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from threading import Lock
from time import perf_counter_ns
from typing import Any, Self, cast

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.evaluation.attempts import (
    EvaluationAttemptBudget,
    EvaluationAttemptSubmission,
    EvaluationFailureCategory,
    StoredEvaluationAttempt,
)
from opsswarm.evaluation.errors import EvaluationBudgetExceeded
from opsswarm.evaluation.metadata import (
    FIXTURE_MULTI_AGENT_PROMPT_VERSION,
    FIXTURE_SINGLE_AGENT_PROMPT_VERSION,
    EvaluationExperimentSpec,
    EvaluationFailureCounts,
    EvaluationKind,
    EvaluationModeTelemetry,
)
from opsswarm.evaluation.models import InvestigationEvaluationDataset
from opsswarm.evaluation.persistence.repository import EvaluationStore
from opsswarm.evaluation.runner import build_mode_benchmark
from opsswarm.investigation.agent import SingleAgentInvestigator
from opsswarm.investigation.errors import (
    InvestigationToolBudgetExceeded,
    MissingStructuredConclusion,
    UnverifiedEvidenceReference,
)
from opsswarm.investigation.models import InvestigationMode, InvestigationRunResult
from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator
from opsswarm.investigation.parallel_model import (
    LangChainConclusionSynthesizer,
    LangChainSpecialistAnalyst,
)
from opsswarm.investigation.scenarios import FrozenScenarioDataSource, InvestigationScenario
from opsswarm.investigation.settings import (
    LlmSettings,
    create_bounded_openai_compatible_model,
)

REAL_MODEL_RUNNER_NAME = "real-model-smoke-v1"
UtcClock = Callable[[], datetime]
MonotonicClock = Callable[[], int]


class RealModelEvaluationSettings(BaseSettings):
    """从本地 .env 读取价格和默认预算，不包含模型密钥字段。"""

    model_config = SettingsConfigDict(env_prefix="EVALUATION_", extra="ignore")

    input_cost_per_million_usd: Decimal = Field(default=Decimal("-1"), ge=0)
    output_cost_per_million_usd: Decimal = Field(default=Decimal("-1"), ge=0)
    max_cost_usd: Decimal = Decimal("0.10")
    timeout_seconds: int = 120
    max_model_requests: int = 6
    max_tool_calls: int = 3
    max_output_tokens_per_request: int = 1024

    @classmethod
    def from_dotenv(cls) -> Self:
        """仅供显式真实评测命令读取项目根目录的 .env。"""

        constructor = cast(Any, cls)
        return cast(Self, constructor(_env_file=".env", _env_file_encoding="utf-8"))

    def to_budget(self) -> EvaluationAttemptBudget:
        """转换为会随每次尝试持久化的不可变预算快照。"""

        return EvaluationAttemptBudget(
            timeout_seconds=self.timeout_seconds,
            max_model_requests=self.max_model_requests,
            max_tool_calls=self.max_tool_calls,
            max_output_tokens_per_request=self.max_output_tokens_per_request,
            max_estimated_cost_usd=self.max_cost_usd,
            input_cost_per_million_usd=self.input_cost_per_million_usd,
            output_cost_per_million_usd=self.output_cost_per_million_usd,
        )


class ModelUsageBudgetHandler(BaseCallbackHandler):
    """在并发模型调用间原子累计请求数、Token 和估算费用。"""

    raise_error = True

    def __init__(self, budget: EvaluationAttemptBudget) -> None:
        super().__init__()
        self._budget = budget
        self._lock = Lock()
        self._model_request_count = 0
        self._input_tokens = 0
        self._output_tokens = 0

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        **kwargs: Any,
    ) -> None:
        """在网络请求前阻止超过次数上限的调用。"""

        del serialized, kwargs
        request_count = len(messages)
        with self._lock:
            if self._model_request_count + request_count > self._budget.max_model_requests:
                raise EvaluationBudgetExceeded("模型请求次数超过显式预算")
            self._model_request_count += request_count

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """按供应商返回的 usage_metadata 累计 Token，并及时检查费用。"""

        del kwargs
        input_tokens = 0
        output_tokens = 0
        for generations in response.generations:
            for generation in generations:
                if not isinstance(generation, ChatGeneration):
                    continue
                message = generation.message
                if not isinstance(message, AIMessage) or message.usage_metadata is None:
                    continue
                input_tokens += int(message.usage_metadata.get("input_tokens", 0))
                output_tokens += int(message.usage_metadata.get("output_tokens", 0))
        with self._lock:
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens
            if self._estimated_cost() > self._budget.max_estimated_cost_usd:
                raise EvaluationBudgetExceeded("模型估算费用超过显式预算")

    def telemetry(
        self,
        *,
        failure_category: EvaluationFailureCategory | None = None,
    ) -> EvaluationModeTelemetry:
        """生成不含模型密钥和提示内容的持久遥测快照。"""

        with self._lock:
            failures = EvaluationFailureCounts(
                **({failure_category.value: 1} if failure_category is not None else {})
            )
            return EvaluationModeTelemetry(
                model_request_count=self._model_request_count,
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                total_tokens=self._input_tokens + self._output_tokens,
                estimated_cost_usd=self._estimated_cost().quantize(
                    Decimal("0.000001"),
                    rounding=ROUND_HALF_UP,
                ),
                failures=failures,
            )

    def _estimated_cost(self) -> Decimal:
        return (
            Decimal(self._input_tokens) * self._budget.input_cost_per_million_usd
            + Decimal(self._output_tokens) * self._budget.output_cost_per_million_usd
        ) / Decimal(1_000_000)


def real_model_experiment_spec(
    experiment_key: str,
    *,
    llm_settings: LlmSettings,
    budget: EvaluationAttemptBudget,
) -> EvaluationExperimentSpec:
    """计算不含密钥的真实模型与预算配置哈希。"""

    payload = {
        "base_url": llm_settings.base_url,
        "temperature": llm_settings.temperature,
        "thinking_mode": llm_settings.thinking_mode,
        "structured_output_method": llm_settings.structured_output_method,
        "budget": budget.model_dump(mode="json"),
    }
    parameters_hash = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return EvaluationExperimentSpec(
        experiment_key=experiment_key,
        evaluation_kind=EvaluationKind.REAL_MODEL,
        model_provider="openai-compatible",
        model_name=llm_settings.model,
        model_parameters_hash=parameters_hash,
        single_agent_prompt_version=FIXTURE_SINGLE_AGENT_PROMPT_VERSION,
        multi_agent_prompt_version=FIXTURE_MULTI_AGENT_PROMPT_VERSION,
    )


class RealModelSmokeRunner:
    """先落库再按固定顺序运行单 Agent 与多 Agent 单场景冒烟。"""

    def __init__(
        self,
        *,
        store: EvaluationStore,
        llm_settings: LlmSettings,
        budget: EvaluationAttemptBudget,
        utc_clock: UtcClock = lambda: datetime.now(UTC),
        monotonic_clock: MonotonicClock = perf_counter_ns,
    ) -> None:
        self._store = store
        self._llm_settings = llm_settings
        self._budget = budget
        self._utc_clock = utc_clock
        self._monotonic_clock = monotonic_clock

    async def run(
        self,
        dataset: InvestigationEvaluationDataset,
        *,
        scenario_id: str,
        experiment_key: str,
        repetition_index: int,
        idempotency_prefix: str,
    ) -> tuple[StoredEvaluationAttempt, StoredEvaluationAttempt]:
        """运行两种模式；任一失败仍保留另一模式的独立审计结果。"""

        scenario = next(
            (item for item in dataset.scenarios if item.scenario_id == scenario_id),
            None,
        )
        if scenario is None:
            raise ValueError(f"评测场景不存在：{scenario_id}")
        experiment = real_model_experiment_spec(
            experiment_key,
            llm_settings=self._llm_settings,
            budget=self._budget,
        )
        modes = (
            (InvestigationMode.SINGLE_AGENT, InvestigationMode.MULTI_AGENT)
            if repetition_index % 2 == 1
            else (InvestigationMode.MULTI_AGENT, InvestigationMode.SINGLE_AGENT)
        )
        attempts: dict[InvestigationMode, StoredEvaluationAttempt] = {}
        for mode in modes:
            attempts[mode] = await self._run_mode(
                dataset=dataset,
                scenario=scenario,
                experiment=experiment,
                repetition_index=repetition_index,
                idempotency_key=f"{idempotency_prefix}:{mode.value}",
                mode=mode,
            )
        return (
            attempts[InvestigationMode.SINGLE_AGENT],
            attempts[InvestigationMode.MULTI_AGENT],
        )

    async def _run_mode(
        self,
        *,
        dataset: InvestigationEvaluationDataset,
        scenario: InvestigationScenario,
        experiment: EvaluationExperimentSpec,
        repetition_index: int,
        idempotency_key: str,
        mode: InvestigationMode,
    ) -> StoredEvaluationAttempt:
        started_at = self._utc_clock()
        creation = await self._store.start_attempt(
            EvaluationAttemptSubmission(
                dataset=dataset,
                experiment=experiment,
                runner_name=REAL_MODEL_RUNNER_NAME,
                repetition_index=repetition_index,
                scenario_id=scenario.scenario_id,
                mode=mode,
                idempotency_key=idempotency_key,
                budget=self._budget,
                started_at=started_at,
            )
        )
        if not creation.created:
            return creation.attempt

        usage = ModelUsageBudgetHandler(self._budget)
        before = self._monotonic_clock()
        try:
            async with asyncio.timeout(self._budget.timeout_seconds):
                result = await self._investigate(mode, scenario, usage)
            duration_ms = max(0, self._monotonic_clock() - before) / 1_000_000
            if result.tool_call_count > self._budget.max_tool_calls:
                raise EvaluationBudgetExceeded("调查工具调用次数超过显式预算")
            benchmark = build_mode_benchmark(mode, result, scenario, duration_ms)
            return await self._store.complete_attempt(
                creation.attempt.id,
                result=benchmark,
                telemetry=usage.telemetry(),
                finished_at=self._utc_clock(),
            )
        except Exception as error:
            category = classify_evaluation_failure(error)
            return await self._store.fail_attempt(
                creation.attempt.id,
                category=category,
                message=redact_failure_message(
                    str(error) or error.__class__.__name__,
                    secrets=(self._llm_settings.api_key.get_secret_value(),),
                ),
                telemetry=usage.telemetry(failure_category=category),
                finished_at=self._utc_clock(),
            )

    async def _investigate(
        self,
        mode: InvestigationMode,
        scenario: InvestigationScenario,
        usage: ModelUsageBudgetHandler,
    ) -> InvestigationRunResult:
        model = create_bounded_openai_compatible_model(
            self._llm_settings,
            max_output_tokens=self._budget.max_output_tokens_per_request,
            timeout_seconds=self._budget.timeout_seconds,
            callbacks=(usage,),
        )
        data_source = FrozenScenarioDataSource(scenario)
        if mode is InvestigationMode.SINGLE_AGENT:
            return await SingleAgentInvestigator(
                model=model,
                data_source=data_source,
                max_tool_calls=self._budget.max_tool_calls,
            ).investigate(scenario.request)
        investigator = LangGraphParallelInvestigator(
            data_source=data_source,
            analyst=LangChainSpecialistAnalyst(
                model,
                structured_output_method=self._llm_settings.structured_output_method,
            ),
            synthesizer=LangChainConclusionSynthesizer(
                model,
                structured_output_method=self._llm_settings.structured_output_method,
            ),
        )
        return (await investigator.investigate(scenario.request)).result


def classify_evaluation_failure(error: Exception) -> EvaluationFailureCategory:
    """把易变供应商异常归一化为稳定、可聚合的失败类别。"""

    if isinstance(error, TimeoutError):
        return EvaluationFailureCategory.TIMEOUT
    if isinstance(error, EvaluationBudgetExceeded):
        return EvaluationFailureCategory.BUDGET_EXCEEDED
    if isinstance(error, InvestigationToolBudgetExceeded):
        return EvaluationFailureCategory.TOOL_ERROR
    if isinstance(error, UnverifiedEvidenceReference):
        return EvaluationFailureCategory.SAFETY_REJECTION
    if isinstance(error, (MissingStructuredConclusion, ValidationError)):
        return EvaluationFailureCategory.STRUCTURED_OUTPUT_ERROR
    return EvaluationFailureCategory.MODEL_REQUEST_ERROR


def redact_failure_message(message: str, *, secrets: tuple[str, ...]) -> str:
    """在持久化前移除已知密钥并限制错误文本长度。"""

    redacted = message
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[已脱敏]")
    normalized = " ".join(redacted.split())
    return (normalized or "未知评测错误")[:500]
