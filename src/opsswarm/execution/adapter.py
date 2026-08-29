"""只允许关闭 release_500 的本地故障实验室适配器。"""

from typing import Protocol

import httpx2

from opsswarm.execution.errors import OperationAdapterFailure
from opsswarm.execution.models import OperationResult
from opsswarm.execution.settings import ExecutionSettings
from opsswarm.fault_lab.models import FaultStatus
from opsswarm.planning import ActionType, RollbackReleaseParameters, TargetType
from opsswarm.planning.persistence import StoredActionStep


class OperationAdapter(Protocol):
    """执行服务唯一依赖的受限异步适配器契约。"""

    async def execute(self, step: StoredActionStep) -> OperationResult: ...


class FaultLabOperationAdapter:
    """把唯一白名单动作映射到固定本地 HTTP 路径。"""

    def __init__(
        self,
        settings: ExecutionSettings | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings if settings is not None else ExecutionSettings()
        self._transport = transport

    async def execute(self, step: StoredActionStep) -> OperationResult:
        """关闭确定性 500 故障；不拼接计划提供的 URL 或命令。"""

        if (
            step.action_type is not ActionType.ROLLBACK_RELEASE
            or step.target_type is not TargetType.SERVICE
            or step.target_name != "order-service"
            or not isinstance(step.parameters, RollbackReleaseParameters)
        ):
            raise OperationAdapterFailure(
                error_code="operation_not_whitelisted",
                message="执行器只允许回滚 order-service 的本地 release_500 故障",
            )
        try:
            async with httpx2.AsyncClient(
                base_url=self._settings.fault_lab_base_url,
                timeout=self._settings.request_timeout_seconds,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = await client.put(
                    "/__admin/faults/release-500",
                    json={"enabled": False},
                )
                response.raise_for_status()
                status = FaultStatus.model_validate(response.json())
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
        if status.enabled:
            raise OperationAdapterFailure(
                error_code="fault_disable_not_confirmed",
                message="故障演练服务未确认关闭 release_500",
            )
        return OperationResult(
            output={
                "fault": "release_500",
                "enabled": False,
                "service": step.target_name,
                "target_version": step.parameters.target_version,
            }
        )
