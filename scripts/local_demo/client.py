"""固定本机演示端口的可信 API 访问与有界状态等待。"""

import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx2

from scripts.local_demo.config import CHANGE, ORDER, ORIGIN


class DemoError(RuntimeError):
    def __init__(self, stage: str, code: str, *, details: dict | None = None) -> None:
        self.stage, self.code = stage, code
        self.details = details or {}
        super().__init__(f"{stage}: {code}")


class DemoClient:
    def __init__(self, tokens: dict, *, transport=None, timeout=180) -> None:
        self.tokens = tokens
        self.timeout = timeout
        self.http = httpx2.Client(
            timeout=15, trust_env=False, follow_redirects=False, transport=transport
        )

    def close(self) -> None:
        self.http.close()

    def _request(
        self,
        path: str,
        body: dict | None,
        *,
        role: str | None,
        method: str | None,
        origin: str,
    ) -> httpx2.Response:
        if (
            origin not in {ORIGIN, ORDER, CHANGE}
            or not path.startswith("/")
            or path.startswith("//")
        ):
            raise DemoError("request", "outside_local_demo")
        verb = method or ("GET" if body is None else "POST")
        headers = (
            {"Authorization": f"Bearer {self.tokens[role]}"} if origin == ORIGIN and role else {}
        )
        try:
            return self.http.request(verb, origin + path, json=body, headers=headers)
        except httpx2.HTTPError as error:
            # 写请求结果未知时交由报告和持久记录核对，不盲目重发。
            raise DemoError(path, "transport_result_unknown") from error

    def call(
        self,
        path: str,
        body: dict | None = None,
        *,
        role="engineer",
        method=None,
        origin=ORIGIN,
        status: int | None = None,
    ) -> Any:
        response = self._request(path, body, role=role, method=method, origin=origin)
        if response.status_code == status:
            return None
        if status is not None or not response.is_success:
            code = f"http_{response.status_code}" if status is None else f"expected_http_{status}"
            details = {}
            if origin == ORIGIN:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                api_code = body.get("error_code") if isinstance(body, dict) else None
                if isinstance(api_code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", api_code):
                    details["api_error_code"] = api_code
            raise DemoError(path, code, details=details)
        return response.json() if response.content else None

    def observe_order_failure(self) -> datetime:
        response = self._request("/orders/local-demo", None, role=None, method="GET", origin=ORDER)
        require(response.status_code == 500, "conditions", "expected_http_500")
        try:
            observed_at = parsedate_to_datetime(response.headers["date"])
        except (KeyError, ValueError, TypeError, OverflowError) as error:
            raise DemoError("conditions", "invalid_order_response_time") from error
        require(
            observed_at.utcoffset() == timedelta(0), "conditions", "invalid_order_response_time"
        )
        return observed_at.astimezone(UTC)

    def poll(self, stage: str, read: Callable[[], Any], ready: Callable[[Any], bool]) -> Any:
        deadline = time.monotonic() + self.timeout
        while True:
            value = read()
            if ready(value):
                return value
            if time.monotonic() >= deadline:
                raise DemoError(stage, "worker_wait_expired")
            time.sleep(1)


def require(condition: bool, stage: str, code: str) -> None:
    if not condition:
        raise DemoError(stage, code)
