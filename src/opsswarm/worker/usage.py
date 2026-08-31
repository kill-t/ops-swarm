"""为串行 Worker 分隔每次调查的模型用量。"""

from decimal import ROUND_HALF_UP, Decimal
from threading import Lock
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from opsswarm.investigation.attempts import ModelPricing, ModelUsageTelemetry


class ModelUsageTracker(BaseCallbackHandler):
    """在并行模型请求间累计用量，并丢弃超时后迟到回调。"""

    def __init__(self, pricing: ModelPricing | None) -> None:
        super().__init__()
        self._pricing = pricing
        self._lock = Lock()
        self._generation = 0
        self._active = False
        self._run_generations: dict[UUID, int] = {}
        self._model_request_count = 0
        self._usage_response_count = 0
        self._input_tokens = 0
        self._output_tokens = 0

    @property
    def pricing(self) -> ModelPricing | None:
        """返回随尝试持久化的价格快照。"""

        return self._pricing

    def begin_attempt(self) -> None:
        """为下一个串行 Worker 任务开启独立计数窗口。"""

        with self._lock:
            if self._active:
                raise RuntimeError("上一次模型用量计数尚未结束")
            self._generation += 1
            # 上一窗口未返回的请求已经失去结算资格，清理映射避免长期超时累积内存。
            self._run_generations.clear()
            self._active = True
            self._model_request_count = 0
            self._usage_response_count = 0
            self._input_tokens = 0
            self._output_tokens = 0

    def finish_attempt(self) -> ModelUsageTelemetry:
        """关闭当前窗口并生成不含提示内容的遥测快照。"""

        with self._lock:
            if not self._active:
                return ModelUsageTelemetry()
            self._active = False
            usage_complete = self._usage_response_count >= self._model_request_count
            estimated_cost = self._estimated_cost() if usage_complete else None
            return ModelUsageTelemetry(
                model_request_count=self._model_request_count,
                usage_complete=usage_complete,
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                total_tokens=self._input_tokens + self._output_tokens,
                estimated_cost_usd=(
                    estimated_cost.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
                    if estimated_cost is not None
                    else None
                ),
            )

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """记录当前尝试发出的聊天模型请求数。"""

        del serialized, kwargs
        with self._lock:
            if not self._active:
                return
            self._run_generations[run_id] = self._generation
            self._model_request_count += len(messages)

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """只将当前计数窗口的 usage metadata 累加进来。"""

        del kwargs
        input_tokens = 0
        output_tokens = 0
        usage_response_count = 0
        for generations in response.generations:
            for generation in generations:
                if not isinstance(generation, ChatGeneration):
                    continue
                message = generation.message
                if not isinstance(message, AIMessage) or message.usage_metadata is None:
                    continue
                usage_response_count += 1
                input_tokens += int(message.usage_metadata.get("input_tokens", 0))
                output_tokens += int(message.usage_metadata.get("output_tokens", 0))
        with self._lock:
            attempt_generation = self._run_generations.pop(run_id, None)
            if not self._active or attempt_generation != self._generation:
                return
            self._usage_response_count += usage_response_count
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """模型失败时清理请求映射，不持久原始异常。"""

        del error, kwargs
        with self._lock:
            self._run_generations.pop(run_id, None)

    def _estimated_cost(self) -> Decimal | None:
        if self._pricing is None:
            return None
        return (
            Decimal(self._input_tokens) * self._pricing.input_cost_per_million_usd
            + Decimal(self._output_tokens) * self._pricing.output_cost_per_million_usd
        ) / Decimal(1_000_000)
