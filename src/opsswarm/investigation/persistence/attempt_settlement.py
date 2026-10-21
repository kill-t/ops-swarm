"""旧调查成功与动态交接共用的父尝试用量结算。"""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from opsswarm.investigation.attempts import InvestigationAttemptStatus, ModelUsageTelemetry
from opsswarm.investigation.errors import WorkItemLeaseLost
from opsswarm.investigation.models import WorkItemError
from opsswarm.investigation.persistence.tables import InvestigationAttemptRow


def finalize_attempt(
    row: InvestigationAttemptRow,
    *,
    status: InvestigationAttemptStatus,
    occurred_at: datetime,
    usage: ModelUsageTelemetry,
    will_retry: bool,
    error: WorkItemError | None,
    model_name: str | None,
) -> None:
    """将运行态尝试只结算一次，并校验价格与费用形状。"""

    if row.status != InvestigationAttemptStatus.RUNNING.value:
        raise WorkItemLeaseLost(row.work_item_id)
    if occurred_at < row.started_at:
        raise ValueError("尝试结束时间不能早于开始时间")
    normalized_model_name = model_name.strip() if model_name is not None else None
    if normalized_model_name is not None and (
        not normalized_model_name or len(normalized_model_name) > 200
    ):
        raise ValueError("model_name 长度必须在 1 到 200 之间")
    if row.model_name is None:
        row.model_name = normalized_model_name
    elif normalized_model_name is not None and row.model_name != normalized_model_name:
        raise ValueError("尝试使用的 model_name 与领取时快照不一致")

    has_pricing = row.input_cost_per_million_usd is not None
    if has_pricing != (row.output_cost_per_million_usd is not None):
        raise RuntimeError("调查尝试价格快照不完整")
    if usage.estimated_cost_usd is not None and not has_pricing:
        raise ValueError("未保存价格快照时不能写入估算费用")
    if (
        status is not InvestigationAttemptStatus.LEASE_EXPIRED
        and usage.usage_complete
        and has_pricing
        and usage.estimated_cost_usd is None
    ):
        raise ValueError("用量与价格均可用时必须写入估算费用")
    if usage.usage_complete and has_pricing:
        assert row.input_cost_per_million_usd is not None
        assert row.output_cost_per_million_usd is not None
        expected_cost = (
            (
                Decimal(usage.input_tokens) * row.input_cost_per_million_usd
                + Decimal(usage.output_tokens) * row.output_cost_per_million_usd
            )
            / Decimal(1_000_000)
        ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        if usage.estimated_cost_usd != expected_cost:
            raise ValueError("估算费用与 Token 及价格快照重算结果不一致")

    row.status = status.value
    row.model_request_count = usage.model_request_count
    row.usage_complete = usage.usage_complete
    row.input_tokens = usage.input_tokens
    row.output_tokens = usage.output_tokens
    row.total_tokens = usage.total_tokens
    row.estimated_cost_usd = usage.estimated_cost_usd
    row.finished_at = occurred_at
    row.duration_ms = int((occurred_at - row.started_at).total_seconds() * 1000)
    row.will_retry = will_retry
    row.error_code = error.error_code if error is not None else None
    row.error_message = error.message if error is not None else None
