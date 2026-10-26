"""固定本地订单 GET 的异步有界探针。"""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

import httpx2

from opsswarm.fault_lab.models import OrderResponse
from opsswarm.verification.candidate.contracts import PROBE_ORDER_ID, PROBE_PATH, ProbeObservation
from opsswarm.verification.candidate.policy import validate_observation
from opsswarm.verification.settings import VerificationSettings

MAX_RESPONSE_BYTES = 16 * 1024


class ProbeFailure(ValueError):
    """稳定脱敏的来源失败，不保存正文、URL 或底层异常。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def utc_now() -> datetime:
    return datetime.now(UTC)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复 JSON 字段")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("拒绝非有限 JSON 数值")


async def _bounded_body(response: httpx2.Response) -> bytes:
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise ProbeFailure("probe_compressed_response")
    length = response.headers.get("content-length")
    if length is not None:
        try:
            valid_length = int(length)
        except ValueError:
            raise ProbeFailure("probe_invalid_response") from None
        if valid_length < 0 or valid_length > MAX_RESPONSE_BYTES:
            raise ProbeFailure("probe_response_too_large")
    result = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=4096):
        if len(result) + len(chunk) > MAX_RESPONSE_BYTES:
            raise ProbeFailure("probe_response_too_large")
        result.extend(chunk)
    return bytes(result)


class Probe:
    """不接受任意路径，不重试，关闭也受单次请求总时限约束。"""

    def __init__(
        self,
        settings: VerificationSettings | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        provided = settings if settings is not None else VerificationSettings()
        if type(provided.request_timeout_seconds) not in (int, float):
            raise ValueError("请求时限不能使用布尔值")
        self._settings = VerificationSettings.model_validate(provided.model_dump())
        timeout = self._settings.request_timeout_seconds
        if type(timeout) not in (int, float) or not 0 < timeout <= 10:
            raise ValueError("请求总时限必须为不超过十秒的正数")
        self._transport, self._clock = transport, clock

    async def observe(self) -> ProbeObservation:
        try:
            requested_at = self._clock()
            async with asyncio.timeout(self._settings.request_timeout_seconds):
                transport = self._transport or httpx2.AsyncHTTPTransport(retries=0)
                async with (
                    httpx2.AsyncClient(
                        base_url=self._settings.fault_lab_base_url,
                        timeout=self._settings.request_timeout_seconds,
                        transport=transport,
                        trust_env=False,
                        follow_redirects=False,
                        headers={"Accept-Encoding": "identity"},
                    ) as client,
                    client.stream("GET", PROBE_PATH) as response,
                ):
                    payload = await _bounded_body(response)
                    status = response.status_code
                order_id: Literal["opsswarm-verification-probe"] | None
                if 500 <= status <= 599:
                    order_id, value = None, 1.0
                elif 200 <= status < 300:
                    raw = json.loads(
                        payload,
                        object_pairs_hook=_unique_object,
                        parse_constant=_reject_constant,
                    )
                    if not isinstance(raw, dict) or set(raw) != set(OrderResponse.model_fields):
                        raise ValueError("缺少明确订单字段")
                    # JSON 模式保留 Decimal 的严格 JSON 数值/字符串语义。
                    order = OrderResponse.model_validate_json(json.dumps(raw), strict=True)
                    if order.order_id != PROBE_ORDER_ID or not order.amount.is_finite():
                        raise ValueError("业务订单身份或金额不合法")
                    order_id, value = PROBE_ORDER_ID, 0.0
                else:
                    raise ProbeFailure("probe_unexpected_status")
                observed_at = self._clock()
                observation = ProbeObservation(
                    requested_at=requested_at,
                    observed_at=observed_at,
                    http_status=status,
                    value=value,
                    order_id=order_id,
                )
                return validate_observation(observation, self._clock())
        except ProbeFailure:
            raise
        except (TimeoutError, httpx2.TimeoutException):
            raise ProbeFailure("probe_timeout") from None
        except httpx2.HTTPError:
            raise ProbeFailure("probe_unavailable") from None
        except (ValueError, TypeError, OverflowError):
            raise ProbeFailure("probe_invalid_response") from None
