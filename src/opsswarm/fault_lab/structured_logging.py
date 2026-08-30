"""故障实验室的有限字段结构化事故日志。"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, TextIO, cast

import structlog
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.fault_lab.controller import FaultSnapshot

ORDER_SERVICE = "order-service"
ORDER_ROUTE = "/orders/{id}"
ACTIVE_RELEASE_VERSION = "2026.01.01-rc1"
CONTAINER_LOG_PATH = "/var/log/opsswarm/order-service.jsonl"


class FaultKind(StrEnum):
    """允许写入事故日志的固定故障分类。"""

    RELEASE_500 = "release_500"
    DATABASE_POOL_TIMEOUT = "database_pool_timeout"
    REDIS_UNAVAILABLE = "redis_unavailable"


class FaultLabLoggingSettings(BaseSettings):
    """结构化日志只允许关闭文件输出或写入项目专用容器路径。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_FAULT_LAB_", extra="ignore")

    log_path: str | None = None

    @field_validator("log_path")
    @classmethod
    def restrict_log_path(cls, value: str | None) -> str | None:
        if value is not None and value != CONTAINER_LOG_PATH:
            raise ValueError("故障实验室日志只能写入项目专用容器路径")
        return value


class _StructuredLogger(Protocol):
    """收窄 structlog 绑定日志器的使用面。"""

    def warning(self, event: str, **event_kw: object) -> Any:
        """输出一条 warning 事件。"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _compact_json(value: object, **kwargs: object) -> str:
    del kwargs
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class FaultLabEventLogger:
    """只输出预定义事故字段，避免原始请求数据和高基数字段泄漏。"""

    def __init__(
        self,
        *,
        settings: FaultLabLoggingSettings | None = None,
        stream: TextIO | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if settings is not None and stream is not None:
            raise ValueError("settings 与 stream 不能同时提供")
        resolved_settings = settings if settings is not None else FaultLabLoggingSettings()
        self._exit_stack = ExitStack()
        output = stream
        if output is None and resolved_settings.log_path is not None:
            # 文件句柄由实例级 ExitStack 在 FastAPI lifespan 结束时统一关闭。
            output = self._exit_stack.enter_context(
                Path(resolved_settings.log_path).open(  # noqa: SIM115
                    "a",
                    encoding="utf-8",
                    buffering=1,
                )
            )
        if output is None:
            output = sys.stdout
        self._logger = cast(
            _StructuredLogger,
            structlog.wrap_logger(
                structlog.PrintLogger(output),
                processors=[
                    structlog.processors.add_log_level,
                    structlog.processors.JSONRenderer(serializer=_compact_json),
                ],
                wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
                cache_logger_on_first_use=True,
            ),
        )
        self._clock = clock
        self._lock = Lock()

    def order_failure(self, *, fault: FaultKind, snapshot: FaultSnapshot) -> None:
        """按故障分类输出固定字段，不记录订单 ID 或任意异常文本。"""

        attributes: dict[str, object] = {
            "timestamp": self._timestamp(),
            "service": ORDER_SERVICE,
            "route": ORDER_ROUTE,
            "status_code": 500,
            "fault": fault.value,
        }
        if fault is FaultKind.RELEASE_500:
            attributes.update(
                error_type="RuntimeError",
                release_version=ACTIVE_RELEASE_VERSION,
            )
        elif fault is FaultKind.DATABASE_POOL_TIMEOUT:
            attributes.update(
                error_type="database_pool_timeout",
                database_pool_size=snapshot.database_pool_size,
                database_pool_waiters=snapshot.database_pool_waiters,
            )
        else:
            attributes.update(
                error_type="redis_connection_unavailable",
                dependency="redis",
            )
        with self._lock:
            self._logger.warning("order_request_failed", **attributes)

    def close(self) -> None:
        """只关闭本实例创建的文件，不关闭注入流或标准输出。"""

        self._exit_stack.close()

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("结构化日志时钟必须返回带时区时间")
        return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
