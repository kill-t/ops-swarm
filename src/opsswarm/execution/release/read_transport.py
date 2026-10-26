"""本地演练只读请求的总超时、响应上限与严格JSON传输。"""

import asyncio
import json
from typing import Any

import httpx2

from opsswarm.execution.settings import ExecutionSettings

MAX_RESPONSE_BYTES = 64 * 1024


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("只读响应包含重复JSON字段")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError("只读响应不能包含非JSON数值")


async def read_json(
    settings: ExecutionSettings,
    transport: httpx2.AsyncBaseTransport | None,
    path: str,
    *,
    allow_not_found: bool = False,
) -> tuple[int, object]:
    async with asyncio.timeout(settings.request_timeout_seconds):
        async with (
            httpx2.AsyncClient(
                base_url=settings.fault_lab_base_url,
                timeout=settings.request_timeout_seconds,
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                headers={"Accept-Encoding": "identity"},
            ) as client,
            client.stream("GET", path) as response,
        ):
            if not (allow_not_found and response.status_code == 404):
                response.raise_for_status()
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("不接受压缩只读响应")
            length = response.headers.get("content-length")
            if length is not None and not 0 <= int(length) <= MAX_RESPONSE_BYTES:
                raise ValueError("只读响应超过上限")
            payload = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError("只读响应超过上限")
                payload.extend(chunk)
        return response.status_code, json.loads(
            payload, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
