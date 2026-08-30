"""订单服务真实 Redis 依赖后端测试。"""

import httpx2
import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError

from opsswarm.fault_lab.controller import RedisAvailabilityConflict
from opsswarm.fault_lab.redis_dependency import (
    FaultLabRedisSettings,
    RemoteRedisDependencyBackend,
)
from opsswarm.fault_lab.redis_proxy import RedisProxyController, create_redis_proxy_app


class FakeRedisClient:
    """避免单元测试访问真实网络。"""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.closed = False

    async def ping(self) -> bool:
        if not self.available:
            raise RedisConnectionError("测试连接失败")
        return True

    async def aclose(self) -> None:
        self.closed = True


def test_settings_reject_external_redis_and_admin_hosts() -> None:
    with pytest.raises(ValidationError, match="Redis 地址只允许"):
        FaultLabRedisSettings(redis_url="redis://example.com:6379/0")

    with pytest.raises(ValidationError, match="Redis 代理管理地址只允许"):
        FaultLabRedisSettings(redis_proxy_admin_url="http://example.com:8474")


@pytest.mark.asyncio
async def test_real_backend_converts_redis_connection_error_to_unavailable() -> None:
    redis_client = FakeRedisClient(available=False)
    backend = RemoteRedisDependencyBackend(redis_client=redis_client)

    assert await backend.is_available() is False

    await backend.close()
    assert redis_client.closed is True


@pytest.mark.asyncio
async def test_real_backend_updates_fixed_proxy_then_pings_redis() -> None:
    controller = RedisProxyController(
        backend_host="127.0.0.1",
        backend_port=6379,
        available=False,
    )
    transport = httpx2.ASGITransport(app=create_redis_proxy_app(controller))
    backend = RemoteRedisDependencyBackend(
        settings=FaultLabRedisSettings(redis_proxy_admin_url="http://redis-proxy:8474"),
        transport=transport,
        redis_client=FakeRedisClient(),
    )

    available = await backend.set_available(
        available=True,
        expected_available=False,
    )

    assert available is True
    assert await controller.is_available() is True
    await backend.close()


@pytest.mark.asyncio
async def test_real_backend_preserves_proxy_compare_and_set_conflict() -> None:
    controller = RedisProxyController(backend_host="127.0.0.1", backend_port=6379)
    transport = httpx2.ASGITransport(app=create_redis_proxy_app(controller))
    backend = RemoteRedisDependencyBackend(
        settings=FaultLabRedisSettings(redis_proxy_admin_url="http://redis-proxy:8474"),
        transport=transport,
        redis_client=FakeRedisClient(),
    )

    with pytest.raises(RedisAvailabilityConflict) as captured:
        await backend.set_available(available=True, expected_available=False)

    assert captured.value.actual_available is True
    await backend.close()
