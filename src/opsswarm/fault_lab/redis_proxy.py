"""仅允许切换 Redis 连通性的固定 TCP 故障代理。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm import __version__
from opsswarm.fault_lab.controller import RedisAvailabilityConflict
from opsswarm.fault_lab.models import (
    RedisAvailabilityConflictResponse,
    RedisDependencyStatus,
    RedisDependencyUpdate,
    RedisProxyHealthResponse,
)

logger = logging.getLogger(__name__)


class RedisProxySettings(BaseSettings):
    """Redis 代理只接受固定后端与受限端口配置。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_REDIS_PROXY_", extra="ignore")

    backend_host: str = Field(default="127.0.0.1", pattern=r"^(127\.0\.0\.1|localhost|lab-redis)$")
    backend_port: int = Field(default=6379, ge=1, le=65535)
    listen_host: str = Field(default="0.0.0.0", pattern=r"^(0\.0\.0\.0|127\.0\.0\.1)$")
    listen_port: int = Field(default=6379, ge=1024, le=65535)
    admin_host: str = Field(default="0.0.0.0", pattern=r"^(0\.0\.0\.0|127\.0\.0\.1)$")
    admin_port: int = Field(default=8474, ge=1024, le=65535)
    connect_timeout_seconds: float = Field(default=1.0, gt=0, le=5)


WriterPair = tuple[asyncio.StreamWriter, asyncio.StreamWriter]


class RedisProxyController:
    """原子控制新连接，并在禁用时关闭所有已有 Redis 连接。"""

    def __init__(
        self,
        *,
        backend_host: str,
        backend_port: int,
        connect_timeout_seconds: float = 1.0,
        available: bool = True,
    ) -> None:
        _validate_availability(available)
        self._backend_host = backend_host
        self._backend_port = backend_port
        self._connect_timeout_seconds = connect_timeout_seconds
        self._available = available
        self._lock = asyncio.Lock()
        self._connections: set[WriterPair] = set()

    async def is_available(self) -> bool:
        async with self._lock:
            return self._available

    async def set_available(
        self,
        *,
        available: bool,
        expected_available: bool | None = None,
    ) -> bool:
        """用比较并设置防止旧审批覆盖新状态。"""

        _validate_availability(available)
        if expected_available is not None:
            _validate_availability(expected_available)
        async with self._lock:
            if expected_available is not None and self._available is not expected_available:
                raise RedisAvailabilityConflict(
                    expected_available=expected_available,
                    actual_available=self._available,
                )
            self._available = available
            connections = tuple(self._connections) if not available else ()

        for client_writer, backend_writer in connections:
            client_writer.close()
            backend_writer.close()
        if connections:
            await asyncio.gather(
                *(self._wait_closed(writer) for pair in connections for writer in pair),
                return_exceptions=True,
            )
        return available

    async def handle_connection(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        """只把字节流转发到启动时确定的 Redis 后端。"""

        if not await self.is_available():
            await self._close_writer(client_writer)
            return

        try:
            backend_reader, backend_writer = await asyncio.wait_for(
                asyncio.open_connection(self._backend_host, self._backend_port),
                timeout=self._connect_timeout_seconds,
            )
        except (OSError, TimeoutError):
            await self._close_writer(client_writer)
            return

        pair = (client_writer, backend_writer)
        async with self._lock:
            if not self._available:
                await self._close_pair(pair)
                return
            self._connections.add(pair)

        try:
            await self._relay_bidirectionally(
                client_reader,
                client_writer,
                backend_reader,
                backend_writer,
            )
        finally:
            async with self._lock:
                self._connections.discard(pair)
            await self._close_pair(pair)

    async def close(self) -> None:
        """关闭代理停止时仍存活的客户端和后端连接。"""

        async with self._lock:
            connections = tuple(self._connections)
            self._connections.clear()
        await asyncio.gather(
            *(self._close_pair(pair) for pair in connections),
            return_exceptions=True,
        )

    @staticmethod
    async def _relay_bidirectionally(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        backend_reader: asyncio.StreamReader,
        backend_writer: asyncio.StreamWriter,
    ) -> None:
        tasks = {
            asyncio.create_task(RedisProxyController._pipe(client_reader, backend_writer)),
            asyncio.create_task(RedisProxyController._pipe(backend_reader, client_writer)),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()

    @staticmethod
    async def _close_pair(pair: WriterPair) -> None:
        for writer in pair:
            writer.close()
        await asyncio.gather(
            *(RedisProxyController._wait_closed(writer) for writer in pair),
            return_exceptions=True,
        )

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter) -> None:
        writer.close()
        await RedisProxyController._wait_closed(writer)

    @staticmethod
    async def _wait_closed(writer: asyncio.StreamWriter) -> None:
        with suppress(OSError):
            await writer.wait_closed()


def _to_status(available: bool) -> RedisDependencyStatus:
    return RedisDependencyStatus(
        available=available,
        redis_unavailable_ratio=0.0 if available else 1.0,
        status="healthy" if available else "unavailable",
    )


def _validate_availability(value: bool) -> None:
    if not isinstance(value, bool):
        raise ValueError("Redis 代理可用性必须是布尔值")


def create_redis_proxy_app(controller: RedisProxyController) -> FastAPI:
    """创建只暴露健康检查和单一状态开关的管理 API。"""

    app = FastAPI(
        title="OpsSwarm Redis Fault Proxy",
        version=__version__,
        description="仅控制本地演练 Redis 的网络连通性，不提供任意代理目标。",
    )

    @app.get("/health", response_model=RedisProxyHealthResponse, tags=["系统"])
    async def get_health() -> RedisProxyHealthResponse:
        return RedisProxyHealthResponse()

    @app.get("/state", response_model=RedisDependencyStatus, tags=["故障控制"])
    async def get_state() -> RedisDependencyStatus:
        return _to_status(await controller.is_available())

    @app.put(
        "/state",
        response_model=RedisDependencyStatus,
        responses={409: {"model": RedisAvailabilityConflictResponse}},
        tags=["故障控制"],
    )
    async def update_state(
        request: RedisDependencyUpdate,
    ) -> RedisDependencyStatus | JSONResponse:
        try:
            available = await controller.set_available(
                available=request.available,
                expected_available=request.expected_available,
            )
        except RedisAvailabilityConflict as error:
            conflict = RedisAvailabilityConflictResponse(
                message=str(error),
                expected_available=error.expected_available,
                actual_available=error.actual_available,
            )
            return JSONResponse(status_code=409, content=conflict.model_dump(mode="json"))
        logger.info(
            "更新 Redis 故障代理 expected_available=%s available=%s",
            request.expected_available,
            request.available,
        )
        return _to_status(available)

    return app


async def serve(settings: RedisProxySettings | None = None) -> None:
    """在同一受限进程中运行 TCP 数据面和 HTTP 管理面。"""

    resolved = settings if settings is not None else RedisProxySettings()
    controller = RedisProxyController(
        backend_host=resolved.backend_host,
        backend_port=resolved.backend_port,
        connect_timeout_seconds=resolved.connect_timeout_seconds,
    )
    tcp_server = await asyncio.start_server(
        controller.handle_connection,
        resolved.listen_host,
        resolved.listen_port,
    )
    web_server = uvicorn.Server(
        uvicorn.Config(
            create_redis_proxy_app(controller),
            host=resolved.admin_host,
            port=resolved.admin_port,
        )
    )
    try:
        await web_server.serve()
    finally:
        tcp_server.close()
        await tcp_server.wait_closed()
        await controller.close()


def main() -> None:
    """启动 Redis 故障代理。"""

    asyncio.run(serve())


if __name__ == "__main__":
    main()
