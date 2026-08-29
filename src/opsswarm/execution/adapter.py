"""只允许版本回滚和数据库连接池配置回滚的本地适配器。"""

from typing import Protocol

import httpx2

from opsswarm.execution.errors import OperationAdapterFailure
from opsswarm.execution.models import OperationResult
from opsswarm.execution.settings import ExecutionSettings
from opsswarm.fault_lab.models import (
    DatabasePoolConflictResponse,
    DatabasePoolStatus,
    FaultStatus,
)
from opsswarm.planning import (
    ActionType,
    ConfigurationKey,
    RollbackConfigurationParameters,
    RollbackReleaseParameters,
    TargetType,
)
from opsswarm.planning.persistence import StoredActionStep


class OperationAdapter(Protocol):
    """执行服务唯一依赖的受限异步适配器契约。"""

    async def execute(self, step: StoredActionStep) -> OperationResult: ...


class FaultLabOperationAdapter:
    """把两种白名单动作映射到各自固定的本地 HTTP 路径。"""

    def __init__(
        self,
        settings: ExecutionSettings | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings if settings is not None else ExecutionSettings()
        self._transport = transport

    async def execute(self, step: StoredActionStep) -> OperationResult:
        """执行固定本地动作；不拼接计划提供的 URL 或命令。"""

        if step.target_type is not TargetType.SERVICE or step.target_name != "order-service":
            raise OperationAdapterFailure(
                error_code="operation_not_whitelisted",
                message="执行器只允许操作本地 order-service",
            )
        try:
            async with httpx2.AsyncClient(
                base_url=self._settings.fault_lab_base_url,
                timeout=self._settings.request_timeout_seconds,
                transport=self._transport,
                trust_env=False,
            ) as client:
                if step.action_type is ActionType.ROLLBACK_RELEASE and isinstance(
                    step.parameters,
                    RollbackReleaseParameters,
                ):
                    return await self._rollback_release(
                        client,
                        parameters=step.parameters,
                        service_name=step.target_name,
                    )
                if (
                    step.action_type is ActionType.ROLLBACK_CONFIGURATION
                    and isinstance(step.parameters, RollbackConfigurationParameters)
                    and step.parameters.key is ConfigurationKey.DATABASE_POOL_SIZE
                ):
                    return await self._rollback_database_pool(
                        client,
                        parameters=step.parameters,
                        service_name=step.target_name,
                    )
                raise OperationAdapterFailure(
                    error_code="operation_not_whitelisted",
                    message="执行器不支持该动作或参数组合",
                )
        except OperationAdapterFailure:
            raise
        except httpx2.TimeoutException as error:
            raise OperationAdapterFailure(
                error_code="fault_lab_timeout",
                message="故障演练服务调用超时",
            ) from error
        except httpx2.HTTPError as error:
            raise OperationAdapterFailure(
                error_code="fault_lab_unavailable",
                message="故障演练服务不可用",
            ) from error
        except (ValueError, TypeError) as error:
            raise OperationAdapterFailure(
                error_code="fault_lab_invalid_response",
                message="故障演练服务返回了无效响应",
            ) from error

    async def _rollback_release(
        self,
        client: httpx2.AsyncClient,
        *,
        parameters: RollbackReleaseParameters,
        service_name: str,
    ) -> OperationResult:
        response = await client.put(
            "/__admin/faults/release-500",
            json={"enabled": False},
        )
        response.raise_for_status()
        status = FaultStatus.model_validate(response.json())
        if status.enabled:
            raise OperationAdapterFailure(
                error_code="fault_disable_not_confirmed",
                message="故障演练服务未确认关闭 release_500",
            )
        return OperationResult(
            output={
                "fault": "release_500",
                "enabled": False,
                "service": service_name,
                "target_version": parameters.target_version,
            }
        )

    async def _rollback_database_pool(
        self,
        client: httpx2.AsyncClient,
        *,
        parameters: RollbackConfigurationParameters,
        service_name: str,
    ) -> OperationResult:
        current_response = await client.get("/__admin/configuration/database-pool")
        current_response.raise_for_status()
        current = DatabasePoolStatus.model_validate(current_response.json())
        if current.value == parameters.target_value:
            return _configuration_result(
                parameters=parameters,
                service_name=service_name,
                status=current,
                already_at_target=True,
            )
        if current.value != parameters.current_value:
            raise OperationAdapterFailure(
                error_code="configuration_current_value_mismatch",
                message=(
                    f"database_pool_size 当前值为 {current.value}，"
                    f"与计划值 {parameters.current_value} 不一致"
                ),
            )

        update_response = await client.put(
            "/__admin/configuration/database-pool",
            json={
                "expected_value": parameters.current_value,
                "value": parameters.target_value,
            },
        )
        if update_response.status_code == 409:
            conflict = DatabasePoolConflictResponse.model_validate(update_response.json())
            raise OperationAdapterFailure(
                error_code="configuration_current_value_mismatch",
                message=(
                    f"database_pool_size 当前值为 {conflict.actual_value}，"
                    f"与计划值 {conflict.expected_value} 不一致"
                ),
            )
        update_response.raise_for_status()
        updated = DatabasePoolStatus.model_validate(update_response.json())
        if updated.value != parameters.target_value:
            raise OperationAdapterFailure(
                error_code="configuration_update_not_confirmed",
                message="故障演练服务未确认数据库连接池目标值",
            )
        return _configuration_result(
            parameters=parameters,
            service_name=service_name,
            status=updated,
            already_at_target=False,
        )


def _configuration_result(
    *,
    parameters: RollbackConfigurationParameters,
    service_name: str,
    status: DatabasePoolStatus,
    already_at_target: bool,
) -> OperationResult:
    return OperationResult(
        output={
            "configuration_key": parameters.key.value,
            "planned_current_value": parameters.current_value,
            "target_value": parameters.target_value,
            "current_value": status.value,
            "database_pool_saturation": status.database_pool_saturation,
            "already_at_target": already_at_target,
            "service": service_name,
        }
    )
