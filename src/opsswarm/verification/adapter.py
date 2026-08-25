"""只读取固定订单端点的本地恢复验证适配器。"""

from typing import Protocol

import httpx2

from opsswarm.fault_lab.models import OrderResponse
from opsswarm.planning import ActionType, TargetType
from opsswarm.planning.persistence import StoredActionStep
from opsswarm.verification.errors import VerificationProbeFailure
from opsswarm.verification.models import VerificationObservation
from opsswarm.verification.settings import VerificationSettings


class VerificationAdapter(Protocol):
    """恢复验证服务唯一依赖的受限异步探针契约。"""

    async def observe(self, step: StoredActionStep) -> VerificationObservation: ...


class FaultLabVerificationAdapter:
    """把固定订单请求映射为单个 5xx 比例样本。"""

    def __init__(
        self,
        settings: VerificationSettings | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings if settings is not None else VerificationSettings()
        self._transport = transport

    async def observe(self, step: StoredActionStep) -> VerificationObservation:
        """访问固定订单 ID；请求目标不接受计划或客户端提供的 URL。"""

        if (
            step.action_type is not ActionType.ROLLBACK_RELEASE
            or step.target_type is not TargetType.SERVICE
            or step.target_name != "order-service"
            or step.verification.signal != "http_5xx_ratio"
        ):
            raise VerificationProbeFailure(
                error_code="verification_not_whitelisted",
                message="恢复探针只允许验证 order-service 的 http_5xx_ratio",
            )
        try:
            async with httpx2.AsyncClient(
                base_url=self._settings.fault_lab_base_url,
                timeout=self._settings.request_timeout_seconds,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = await client.get("/orders/opsswarm-verification-probe")
        except httpx2.TimeoutException as error:
            raise VerificationProbeFailure(
                error_code="verification_probe_timeout",
                message="恢复验证探针调用超时",
            ) from error
        except httpx2.HTTPError as error:
            raise VerificationProbeFailure(
                error_code="verification_probe_unavailable",
                message="恢复验证探针不可用",
            ) from error

        if 500 <= response.status_code < 600:
            return VerificationObservation(
                signal="http_5xx_ratio",
                value=1.0,
                http_status=response.status_code,
            )
        if 200 <= response.status_code < 300:
            try:
                order = OrderResponse.model_validate(response.json())
            except (ValueError, TypeError) as error:
                raise VerificationProbeFailure(
                    error_code="verification_probe_invalid_response",
                    message="恢复验证探针返回了无效响应",
                    http_status=response.status_code,
                ) from error
            if order.order_id != "opsswarm-verification-probe":
                raise VerificationProbeFailure(
                    error_code="verification_probe_invalid_response",
                    message="恢复验证探针返回了不匹配的订单",
                    http_status=response.status_code,
                )
            return VerificationObservation(
                signal="http_5xx_ratio",
                value=0.0,
                http_status=response.status_code,
            )
        raise VerificationProbeFailure(
            error_code="verification_probe_unexpected_status",
            message="恢复验证探针返回了非预期状态",
            http_status=response.status_code,
        )
