"""Redis TCP 故障代理测试。"""

import asyncio

import httpx2
import pytest

from opsswarm.fault_lab.redis_proxy import RedisProxyController, create_redis_proxy_app


async def _echo(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while data := await reader.read(1024):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_proxy_forwards_bytes_and_disabling_closes_existing_connection() -> None:
    backend = await asyncio.start_server(_echo, "127.0.0.1", 0)
    backend_port = int(backend.sockets[0].getsockname()[1])
    controller = RedisProxyController(
        backend_host="127.0.0.1",
        backend_port=backend_port,
    )
    proxy = await asyncio.start_server(controller.handle_connection, "127.0.0.1", 0)
    proxy_port = int(proxy.sockets[0].getsockname()[1])

    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(b"PING")
    await writer.drain()

    assert await reader.readexactly(4) == b"PING"

    await controller.set_available(available=False, expected_available=True)
    assert await asyncio.wait_for(reader.read(), timeout=1) == b""

    writer.close()
    await writer.wait_closed()
    proxy.close()
    backend.close()
    await proxy.wait_closed()
    await backend.wait_closed()
    await controller.close()


@pytest.mark.asyncio
async def test_unavailable_proxy_rejects_new_connection_and_can_recover() -> None:
    backend = await asyncio.start_server(_echo, "127.0.0.1", 0)
    backend_port = int(backend.sockets[0].getsockname()[1])
    controller = RedisProxyController(
        backend_host="127.0.0.1",
        backend_port=backend_port,
        available=False,
    )
    proxy = await asyncio.start_server(controller.handle_connection, "127.0.0.1", 0)
    proxy_port = int(proxy.sockets[0].getsockname()[1])

    blocked_reader, blocked_writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    assert await asyncio.wait_for(blocked_reader.read(), timeout=1) == b""
    blocked_writer.close()
    await blocked_writer.wait_closed()

    await controller.set_available(available=True, expected_available=False)
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(b"PONG")
    await writer.drain()
    assert await reader.readexactly(4) == b"PONG"

    writer.close()
    await writer.wait_closed()
    proxy.close()
    backend.close()
    await proxy.wait_closed()
    await backend.wait_closed()
    await controller.close()


@pytest.mark.asyncio
async def test_proxy_admin_api_enforces_compare_and_set() -> None:
    controller = RedisProxyController(backend_host="127.0.0.1", backend_port=6379)
    transport = httpx2.ASGITransport(app=create_redis_proxy_app(controller))

    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        changed = await client.put(
            "/state",
            json={"expected_available": True, "available": False},
        )
        conflict = await client.put(
            "/state",
            json={"expected_available": True, "available": True},
        )
        current = await client.get("/state")

    assert changed.status_code == 200
    assert changed.json()["available"] is False
    assert conflict.status_code == 409
    assert conflict.json()["actual_available"] is False
    assert current.json() == {
        "dependency": "redis",
        "available": False,
        "redis_unavailable_ratio": 1.0,
        "status": "unavailable",
    }


@pytest.mark.asyncio
async def test_proxy_admin_api_rejects_non_boolean_state() -> None:
    controller = RedisProxyController(backend_host="127.0.0.1", backend_port=6379)
    transport = httpx2.ASGITransport(app=create_redis_proxy_app(controller))

    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put("/state", json={"available": 0})

    assert response.status_code == 422


@pytest.mark.parametrize("value", [0, 1, "true"])
def test_proxy_controller_rejects_non_boolean_state(value: object) -> None:
    controller = RedisProxyController(backend_host="127.0.0.1", backend_port=6379)

    with pytest.raises(ValueError, match="必须是布尔值"):
        asyncio.run(controller.set_available(available=value))  # type: ignore[arg-type]
