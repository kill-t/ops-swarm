"""order-service 使用的 Redis 依赖端口。"""

from __future__ import annotations

from typing import Protocol
from urllib.parse import urlsplit

import httpx2
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio import Redis
from redis.exceptions import RedisError

from opsswarm.fault_lab.controller import FaultController, RedisAvailabilityConflict
from opsswarm.fault_lab.models import (
    RedisAvailabilityConflictResponse,
    RedisDependencyStatus,
    RedisDependencyUpdate,
)

DEFAULT_REDIS_URL = "redis://127.0.0.1:16379/0"
DEFAULT_REDIS_PROXY_ADMIN_URL = "http://127.0.0.1:18474"
ALLOWED_REDIS_HOSTS = frozenset({"127.0.0.1", "localhost", "redis-proxy"})


class FaultLabRedisSettings(BaseSettings):
    """限制订单服务只能连接项目内 Redis 与固定管理接口。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_FAULT_LAB_", extra="ignore")

    redis_url: str = Field(default=DEFAULT_REDIS_URL, min_length=1)
    redis_proxy_admin_url: str = Field(default=DEFAULT_REDIS_PROXY_ADMIN_URL, min_length=1)
    timeout_seconds: float = Field(default=1.0, gt=0, le=5)

    @field_validator("redis_url")
    @classmethod
    def restrict_redis_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "redis":
            raise ValueError("Redis 地址只允许 redis 协议")
        if parsed.hostname not in ALLOWED_REDIS_HOSTS:
            raise ValueError("Redis 地址只允许 localhost、127.0.0.1 或 redis-proxy")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("演练 Redis 地址不能包含用户信息")
        if parsed.path not in {"", "/", "/0"} or parsed.query or parsed.fragment:
            raise ValueError("演练 Redis 地址只允许使用 0 号数据库且不能包含额外参数")
        return value

    @field_validator("redis_proxy_admin_url")
    @classmethod
    def restrict_admin_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme != "http":
            raise ValueError("Redis 代理管理地址只允许 http")
        if parsed.hostname not in ALLOWED_REDIS_HOSTS:
            raise ValueError("Redis 代理管理地址只允许本机或 redis-proxy")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Redis 代理管理地址不能包含用户信息")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Redis 代理管理地址不能包含路径、查询参数或片段")
        return normalized


class RedisDependencyBackend(Protocol):
    """隔离内存测试状态与真实 Redis 网络访问。"""

    async def is_available(self) -> bool:
        """返回订单请求是否真的能够访问 Redis。"""

    async def set_available(
        self,
        *,
        available: bool,
        expected_available: bool | None = None,
    ) -> bool:
        """通过比较并设置切换受控代理，并返回实际可用性。"""

    async def close(self) -> None:
        """释放持有的网络资源。"""


class RedisPingClient(Protocol):
    """收窄 redis-py 在本模块中需要的能力，便于无网络测试。"""

    async def ping(self) -> bool:
        """发送 PING。"""

    async def aclose(self) -> None:
        """关闭连接池。"""


class LocalRedisDependencyBackend:
    """单元测试使用的确定性内存后端。"""

    def __init__(self, controller: FaultController) -> None:
        self._controller = controller

    async def is_available(self) -> bool:
        return self._controller.snapshot().redis_available

    async def set_available(
        self,
        *,
        available: bool,
        expected_available: bool | None = None,
    ) -> bool:
        snapshot = self._controller.set_redis_available(
            available=available,
            expected_available=expected_available,
        )
        return snapshot.redis_available

    async def close(self) -> None:
        return None


class RemoteRedisDependencyBackend:
    """经固定 TCP 代理探测真实 Redis，并经固定管理 API 切换故障。"""

    def __init__(
        self,
        settings: FaultLabRedisSettings | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        redis_client: RedisPingClient | None = None,
    ) -> None:
        self._settings = settings if settings is not None else FaultLabRedisSettings()
        self._redis: RedisPingClient = redis_client or Redis.from_url(
            self._settings.redis_url,
            socket_connect_timeout=self._settings.timeout_seconds,
            socket_timeout=self._settings.timeout_seconds,
        )
        self._transport = transport

    async def is_available(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except (RedisError, OSError, TimeoutError):
            return False

    async def set_available(
        self,
        *,
        available: bool,
        expected_available: bool | None = None,
    ) -> bool:
        request = RedisDependencyUpdate(
            available=available,
            expected_available=expected_available,
        )
        async with httpx2.AsyncClient(
            base_url=self._settings.redis_proxy_admin_url,
            timeout=self._settings.timeout_seconds,
            transport=self._transport,
        ) as client:
            response = await client.put(
                "/state",
                json=request.model_dump(mode="json", exclude_none=True),
            )

        if response.status_code == 409:
            conflict = RedisAvailabilityConflictResponse.model_validate(response.json())
            raise RedisAvailabilityConflict(
                expected_available=conflict.expected_available,
                actual_available=conflict.actual_available,
            )
        response.raise_for_status()
        RedisDependencyStatus.model_validate(response.json())
        return await self.is_available()

    async def close(self) -> None:
        await self._redis.aclose()
