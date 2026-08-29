"""只读取固定订单或连接池状态端点的本地恢复验证适配器。"""

from typing import Protocol

import httpx2

from opsswarm.fault_lab.models import DatabasePoolStatus, OrderResponse
from opsswarm.planning import (
    ActionType,
    ConfigurationKey,
    RollbackConfigurationParameters,
    RollbackReleaseParameters,
    TargetType,
)
from opsswarm.planning.persistence import StoredActionStep
from opsswarm.verification.errors import VerificationProbeFailure
from opsswarm.verification.models import VerificationObservation
from opsswarm.verification.settings import VerificationSettings


class VerificationAdapter(Protocol):
    """恢复验证服务唯一依赖的受限异步探针契约。"""

    async def observe(self, step: StoredActionStep) -> VerificationObservation: ...


class FaultLabVerificationAdapter:
    """把固定只读端点映射为对应动作的恢复样本。"""

    def __init__(
        self,
        settings: VerificationSettings | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings if settings is not None else VerificationSettings()
        self._transport = transport

    async def observe(self, step: StoredActionStep) -> VerificationObservation:
        """访问固定探针路径；请求目标不接受计划或客户端提供的 URL。"""

        if step.target_type is not TargetType.SERVICE or step.target_name != "order-service":
            raise VerificationProbeFailure(
                error_code="verification_not_whitelisted",
                message="恢复探针只允许读取本地 order-service",
            )
        try:
            async with httpx2.AsyncClient(
                base_url=self._settings.fault_lab_base_url,
                timeout=self._settings.request_timeout_seconds,
                transport=self._transport,
                trust_env=False,
            ) as client:
                if (
                    step.action_type is ActionType.ROLLBACK_RELEASE
                    and isinstance(step.parameters, RollbackReleaseParameters)
                    and step.verification.signal == "http_5xx_ratio"
                ):
                    return await self._observe_http_5xx_ratio(client)
                if (
                    step.action_type is ActionType.ROLLBACK_CONFIGURATION
                    and isinstance(step.parameters, RollbackConfigurationParameters)
                    and step.parameters.key is ConfigurationKey.DATABASE_POOL_SIZE
                    and step.verification.signal == "database_pool_saturation"
                ):
                    return await self._observe_database_pool_saturation(client)
                raise VerificationProbeFailure(
                    error_code="verification_not_whitelisted",
                    message="恢复探针不支持该动作、参数或信号组合",
                )
        except VerificationProbeFailure:
            raise
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

    async def _observe_http_5xx_ratio(
        self,
        client: httpx2.AsyncClient,
    ) -> VerificationObservation:
        response = await client.get("/orders/opsswarm-verification-probe")
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

    async def _observe_database_pool_saturation(
        self,
        client: httpx2.AsyncClient,
    ) -> VerificationObservation:
        response = await client.get("/__admin/configuration/database-pool")
        if not 200 <= response.status_code < 300:
            raise VerificationProbeFailure(
                error_code="verification_probe_unexpected_status",
                message="连接池恢复探针返回了非预期状态",
                http_status=response.status_code,
            )
        try:
            status = DatabasePoolStatus.model_validate(response.json())
        except (ValueError, TypeError) as error:
            raise VerificationProbeFailure(
                error_code="verification_probe_invalid_response",
                message="连接池恢复探针返回了无效响应",
                http_status=response.status_code,
            ) from error
        return VerificationObservation(
            signal="database_pool_saturation",
            value=status.database_pool_saturation,
            http_status=response.status_code,
        )
