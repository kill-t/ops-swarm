"""发布准备只读观测的完整来源、有界网络与失败关闭。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from opsswarm.execution.release.contracts import ReleaseRequestError
from opsswarm.execution.release.observer import MAX_RESPONSE_BYTES, FaultLabReleaseObserver
from opsswarm.execution.settings import ExecutionSettings

NOW = datetime(2026, 10, 26, 2, tzinfo=UTC)
PATH = "/__admin/simulated-release"


def state(**changes):
    return {
        "service_name": "order-service",
        "source": "local-release-simulation-v1",
        "simulation": True,
        "version": "2026.01.01-rc1",
        "revision": 2,
        "command_count": 1,
        "release_500_enabled": True,
        "updated_at": (NOW - timedelta(seconds=1)).isoformat(),
        **changes,
    }


def observer(handler, **kwargs):
    return FaultLabReleaseObserver(
        transport=httpx2.MockTransport(handler), clock=lambda: NOW, **kwargs
    )


@pytest.mark.asyncio
async def test_fixed_origin_method_path_and_complete_identity():
    calls = []

    def handler(request):
        calls.append((request.method, str(request.url), request.content))
        return httpx2.Response(200, json=state())

    result = await observer(handler).observe()
    assert result.observed_at == NOW and result.source_path == PATH
    assert result.state.version == "2026.01.01-rc1" and result.state.revision == 2
    assert result.state.simulation is True
    assert calls == [("GET", "http://127.0.0.1:18001" + PATH, b"")]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 307, 308, 404, 500, 503])
async def test_http_failure_does_not_follow_redirect_or_copy_private_details(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(
            status, headers={"location": "https://external.invalid/private"}, text="secret-token"
        )

    with pytest.raises(ReleaseRequestError) as failure:
        await observer(handler).observe()
    assert failure.value.status_code == 409
    assert len(calls) == 1 and "secret" not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"simulation": 1},
        {"simulation": "true"},
        {"simulation": False},
        {"source": "deployment-environment"},
        {"service_name": "another-service"},
        {"revision": True},
        {"revision": "2"},
        {"revision": 2.0},
        {"revision": 0},
        {"command_count": True},
        {"command_count": -1},
        {"release_500_enabled": 1},
        {"release_500_enabled": "true"},
        {"release_500_enabled": False},
        {"version": "latest"},
        {"updated_at": "2026-10-26T02:00:00"},
        {"updated_at": (NOW + timedelta(seconds=1)).isoformat()},
        {"updated_at": 12345},
        {"unknown": "injected"},
    ],
)
async def test_wrong_shape_coercion_or_future_state_is_not_usable_observation(change):
    with pytest.raises(ReleaseRequestError):
        await observer(lambda _: httpx2.Response(200, json=state(**change))).observe()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["service_name", "source", "simulation", "updated_at"])
async def test_missing_identity_is_not_filled_from_dto_defaults(field):
    payload = state()
    payload.pop(field)
    with pytest.raises(ReleaseRequestError):
        await observer(lambda _: httpx2.Response(200, json=payload)).observe()


class Chunks(httpx2.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.reads, self.closed = chunks, 0, False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_stream_limit_stops_reading_and_closes():
    stream = Chunks([b" " * 8192] * 20)
    with pytest.raises(ReleaseRequestError):
        await observer(lambda _: httpx2.Response(200, stream=stream)).observe()
    assert stream.reads == MAX_RESPONSE_BYTES // 8192 + 1
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("length", ["65537", "-1", "invalid"])
async def test_invalid_content_length_rejects_before_body(length):
    stream = Chunks([b"private"])
    with pytest.raises(ReleaseRequestError):
        await observer(
            lambda _: httpx2.Response(200, headers={"content-length": length}, stream=stream)
        ).observe()
    assert stream.reads == 0 and stream.closed


@pytest.mark.asyncio
async def test_compression_is_rejected_before_body():
    stream = Chunks([b"not compressed"])
    with pytest.raises(ReleaseRequestError):
        await observer(
            lambda _: httpx2.Response(200, headers={"content-encoding": "gzip"}, stream=stream)
        ).observe()
    assert stream.reads == 0 and stream.closed


@pytest.mark.asyncio
async def test_exact_byte_limit_is_accepted_without_truncation():
    payload = json.dumps(state()).encode()
    payload += b" " * (MAX_RESPONSE_BYTES - len(payload))
    result = await observer(lambda _: httpx2.Response(200, content=payload)).observe()
    assert result.state.revision == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx2.ConnectError, httpx2.ReadTimeout])
async def test_network_errors_are_bounded_and_private_details_stay_private(failure):
    def handler(request):
        raise failure("credential=secret", request=request)

    with pytest.raises(ReleaseRequestError) as caught:
        await observer(handler).observe()
    assert "credential" not in str(caught.value) and "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_total_timeout_covers_transport_ignoring_client_timeout():
    entered = asyncio.Event()

    async def handler(_):
        entered.set()
        await asyncio.Event().wait()

    bounded = observer(handler, settings=ExecutionSettings(request_timeout_seconds=0.01))
    with pytest.raises(ReleaseRequestError):
        await asyncio.wait_for(bounded.observe(), timeout=1)
    assert entered.is_set()


@pytest.mark.asyncio
async def test_total_timeout_covers_body_and_closes_stream():
    class Slow(httpx2.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"{"
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    stream = Slow()
    bounded = observer(
        lambda _: httpx2.Response(200, stream=stream),
        settings=ExecutionSettings(request_timeout_seconds=0.01),
    )
    with pytest.raises(ReleaseRequestError):
        await asyncio.wait_for(bounded.observe(), timeout=1)
    assert stream.closed


def test_copied_settings_cannot_bypass_local_host_guard():
    invalid = ExecutionSettings().model_copy(
        update={"fault_lab_base_url": "https://remote.invalid"}
    )
    with pytest.raises(ValueError):
        FaultLabReleaseObserver(invalid)


@pytest.mark.asyncio
async def test_ambient_proxy_is_explicitly_disabled(monkeypatch):
    original = httpx2.AsyncClient
    observed = []

    def client(**kwargs):
        observed.append((kwargs["trust_env"], kwargs["follow_redirects"], kwargs["headers"]))
        return original(**kwargs)

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setattr(httpx2, "AsyncClient", client)
    await observer(lambda _: httpx2.Response(200, json=state())).observe()
    assert observed == [(False, False, {"Accept-Encoding": "identity"})]
