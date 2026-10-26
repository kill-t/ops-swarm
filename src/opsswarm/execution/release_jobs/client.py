"""固定本地发布窗口的单次派发、只读查询与完整材料校验。"""

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Self

import httpx2
from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator, model_validator

from opsswarm.execution.release.observer import utc_now
from opsswarm.execution.settings import ExecutionSettings
from opsswarm.fault_lab.release.contracts import ReleaseState
from opsswarm.fault_lab.release.dispatch_contracts import (
    DispatchCommand,
    DispatchRecord,
    DispatchView,
)
from opsswarm.fault_lab.release.policy import command_hash
from opsswarm.fault_lab.release.receipts import ReleaseReceipt, receipt_time

DISPATCH_PATH = "/__admin/simulated-release/dispatches"
MAX_RESPONSE_BYTES = 64 * 1024


class DispatchClientError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DispatchObservation(BaseModel):
    """保留精确查询路径及采集时刻，历史重建也执行同样的严格校验。"""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    view: DispatchView
    observed_at: AwareDatetime
    source_path: str

    @field_validator("observed_at", mode="before")
    @classmethod
    def require_time(cls, value: object) -> datetime:
        return receipt_time(value)

    @field_validator("view", mode="before")
    @classmethod
    def require_complete_view(cls, value: object) -> object:
        raw = value.model_dump() if isinstance(value, DispatchView) else value
        if not isinstance(raw, dict) or set(raw) != set(DispatchView.model_fields):
            raise ValueError("派发视图必须包含完整字段")
        record = raw["record"]
        if not isinstance(record, dict) or set(record) != set(DispatchRecord.model_fields):
            raise ValueError("派发记录必须包含完整字段")
        receipt = raw["receipt"]
        if not isinstance(receipt, dict) or set(receipt) != set(ReleaseReceipt.model_fields):
            raise ValueError("派发回执必须包含完整字段")
        state = receipt["current"]
        if not isinstance(state, dict) or set(state) != set(ReleaseState.model_fields):
            raise ValueError("派发当前状态必须显式声明完整来源")
        return {**raw, "checked_at": receipt_time(raw["checked_at"])}

    @model_validator(mode="after")
    def require_consistent_material(self) -> Self:
        view, observed = self.view, self.observed_at
        record, receipt = view.record, view.receipt
        checked = view.checked_at
        if (
            self.source_path not in {DISPATCH_PATH, f"{DISPATCH_PATH}/{record.operation_id}"}
            or checked > observed
            or observed - checked >= timedelta(seconds=30)
            or checked < record.created_at
            or checked < receipt.current.updated_at
            or (record.finished_at is not None and checked < record.finished_at)
            or receipt.idempotency_key != record.command.idempotency_key
            or record.command_sha256 != command_hash(record.command.model_dump(mode="json"))
        ):
            raise ValueError("派发材料的来源、时间或命令关联不一致")
        expected = (
            record.status
            if record.status != "prepared"
            else ("ready" if checked < record.expires_at else "expired")
        )
        if view.availability != expected:
            raise ValueError("派发状态与窗口投影不一致")
        command = receipt.command
        if record.status == "applied":
            if (
                command is None
                or command.id != record.command_id
                or command.kind != "cas"
                or command.command != record.command
                or command.command_sha256 != record.command_sha256
                or command.created_at != record.finished_at
                or not record.created_at <= command.created_at < record.expires_at
            ):
                raise ValueError("已应用派发缺少原窗口内的精确成功回执")
        elif command is not None:
            raise ValueError("未应用派发不能携带成功回执")
        return self


def validate_observation(
    command: DispatchCommand, observation: DispatchObservation, *, now: datetime
) -> DispatchObservation:
    """将远端材料绑定原命令与原窗口；不判断业务批准或授予写入资格。"""

    try:
        expected = DispatchCommand.model_validate(command.model_dump())
        checked = DispatchObservation.model_validate(observation.model_dump())
        current = receipt_time(now)
        actual = DispatchCommand.model_validate(
            checked.view.record.model_dump(include=set(DispatchCommand.model_fields))
        )
        if (
            actual != expected
            or checked.observed_at > current
            or current - checked.observed_at >= timedelta(seconds=30)
            or current - checked.view.checked_at >= timedelta(seconds=30)
        ):
            raise ValueError("材料不是原操作或已超过30秒时效")
        return checked
    except (ValueError, TypeError, AttributeError, OverflowError) as error:
        raise DispatchClientError("release_dispatch_invalid", "发布派发材料无法严格校验") from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("派发响应包含重复JSON字段")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError("派发响应不能包含非JSON数值")


class ReleaseDispatchClient:
    def __init__(
        self,
        settings: ExecutionSettings | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        provided = settings if settings is not None else ExecutionSettings()
        self._settings = ExecutionSettings.model_validate(provided.model_dump())
        self._transport, self._clock = transport, clock

    async def register(self, command: DispatchCommand) -> DispatchObservation:
        return await self._request("POST", command)

    async def execute(self, command: DispatchCommand) -> DispatchObservation:
        return await self._request("PUT", command)

    async def lookup(self, command: DispatchCommand) -> DispatchObservation:
        return await self._request("GET", command)

    async def _request(self, method: str, command: DispatchCommand) -> DispatchObservation:
        try:
            checked = DispatchCommand.model_validate(command.model_dump())
            path = DISPATCH_PATH if method == "POST" else f"{DISPATCH_PATH}/{checked.operation_id}"
            # HTTP传输默认不重试；外部调用方不能在不确定结果后重发写请求。
            transport = (
                self._transport
                if self._transport is not None
                else httpx2.AsyncHTTPTransport(retries=0)
            )
            async with asyncio.timeout(self._settings.request_timeout_seconds):
                async with (
                    httpx2.AsyncClient(
                        base_url=self._settings.fault_lab_base_url,
                        timeout=self._settings.request_timeout_seconds,
                        transport=transport,
                        trust_env=False,
                        follow_redirects=False,
                        headers={"Accept-Encoding": "identity"},
                    ) as client,
                    client.stream(
                        method,
                        path,
                        json=checked.model_dump(mode="json") if method == "POST" else None,
                    ) as response,
                ):
                    if response.status_code not in ({200} if method == "GET" else {200, 201}):
                        raise DispatchClientError(
                            "release_dispatch_unavailable", "发布派发请求未取得有效响应"
                        )
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ValueError("派发响应不允许压缩")
                    length = response.headers.get("content-length")
                    if length is not None and not 0 <= int(length) <= MAX_RESPONSE_BYTES:
                        raise ValueError("派发响应大小无效")
                    payload = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ValueError("派发响应超过64KiB")
                        payload.extend(chunk)
                raw = json.loads(
                    payload, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
                )
                if method != "GET":
                    if (
                        not isinstance(raw, dict)
                        or set(raw) != {"applied", "dispatch"}
                        or type(raw["applied"]) is not bool
                        or raw["applied"] is not (response.status_code == 201)
                    ):
                        raise ValueError("派发结果封套与HTTP状态不一致")
                    raw = raw["dispatch"]
                observation = DispatchObservation(
                    view=raw, observed_at=self._clock(), source_path=path
                )
                result = validate_observation(checked, observation, now=self._clock())
                if method == "PUT" and result.view.record.status != "applied":
                    raise ValueError("执行响应必须携带已应用操作")
                return result
        except DispatchClientError:
            raise
        except (TimeoutError, httpx2.TimeoutException) as error:
            raise DispatchClientError("release_dispatch_timeout", "发布派发请求超时") from error
        except httpx2.HTTPError as error:
            raise DispatchClientError(
                "release_dispatch_unavailable", "发布派发请求不可用"
            ) from error
        except (ValueError, TypeError, AttributeError, OverflowError) as error:
            raise DispatchClientError(
                "release_dispatch_invalid", "发布派发响应无法严格校验"
            ) from error
