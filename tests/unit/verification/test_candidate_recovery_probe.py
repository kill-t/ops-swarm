"""固定业务探针的网络上限、严格正文与脱敏失败测试。"""

import asyncio
from datetime import UTC, datetime

import httpx2
import pytest

from opsswarm.fault_lab.app import create_app
from opsswarm.fault_lab.controller import FaultController
from opsswarm.verification.candidate.probe import MAX_RESPONSE_BYTES, Probe, ProbeFailure
from opsswarm.verification.settings import VerificationSettings

NOW = datetime(2026, 10, 26, tzinfo=UTC)
GOOD_BODY = (
    b'{"order_id":"opsswarm-verification-probe","status":"confirmed",'
    b'"amount":"99.00","currency":"CNY"}'
)


def settings(**changes: object) -> VerificationSettings:
    return VerificationSettings.model_validate(
        {"fault_lab_base_url": "http://order-service", **changes}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_actual_local_order_endpoint(enabled: bool) -> None:
    controller = FaultController()
    controller.set_release_500(enabled=enabled)
    probe = Probe(
        settings(), transport=httpx2.ASGITransport(app=create_app(controller)), clock=lambda: NOW
    )
    observed = await probe.observe()
    assert observed.http_status == (500 if enabled else 200)
    assert observed.value == (1.0 if enabled else 0.0)
    assert observed.order_id == (None if enabled else "opsswarm-verification-probe")


@pytest.mark.asyncio
async def test_single_fixed_get_no_credentials_redirects_or_compression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    requests = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url) == "http://order-service/orders/opsswarm-verification-probe"
        assert request.headers["accept-encoding"] == "identity"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx2.Response(200, content=GOOD_BODY)

    observed = await Probe(
        settings(), transport=httpx2.MockTransport(handler), clock=lambda: NOW
    ).observe()
    assert observed.requested_at == observed.observed_at == NOW
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "headers", "code"),
    [
        (302, b"", {"location": "http://localhost/other"}, "probe_unexpected_status"),
        (404, b"private error", {}, "probe_unexpected_status"),
        (200, b'{"order_id":"opsswarm-verification-probe"}', {}, "probe_invalid_response"),
        (200, GOOD_BODY.replace(b"confirmed", b"pending"), {}, "probe_invalid_response"),
        (200, GOOD_BODY.replace(b"CNY", b"USD"), {}, "probe_invalid_response"),
        (
            200,
            GOOD_BODY.replace(b"opsswarm-verification-probe", b"other"),
            {},
            "probe_invalid_response",
        ),
        (
            200,
            GOOD_BODY.replace(b'"amount":"99.00"', b'"amount":NaN'),
            {},
            "probe_invalid_response",
        ),
        (
            200,
            GOOD_BODY.replace(b'"amount":"99.00"', b'"amount":1e9999'),
            {},
            "probe_invalid_response",
        ),
        (
            200,
            GOOD_BODY.replace(b'"amount":"99.00"', b'"amount":true'),
            {},
            "probe_invalid_response",
        ),
        (
            200,
            GOOD_BODY.replace(b'"amount":"99.00"', b'"amount":"99.00","amount":"1"'),
            {},
            "probe_invalid_response",
        ),
        (200, b"private invalid payload", {}, "probe_invalid_response"),
        (200, GOOD_BODY, {"content-encoding": "gzip"}, "probe_compressed_response"),
        (
            200,
            GOOD_BODY,
            {"content-length": str(MAX_RESPONSE_BYTES + 1)},
            "probe_response_too_large",
        ),
        (200, GOOD_BODY, {"content-length": "-1"}, "probe_response_too_large"),
        (200, GOOD_BODY, {"content-length": "invalid"}, "probe_invalid_response"),
    ],
)
async def test_invalid_responses_fail_closed_without_body_in_error(
    status: int, body: bytes, headers: dict[str, str], code: str
) -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        # 测试压缩护栏时不让 httpx 在构造预读响应时先做解压。
        return httpx2.Response(status, stream=Chunks(body), headers=headers)

    probe = Probe(settings(), transport=httpx2.MockTransport(handler), clock=lambda: NOW)
    with pytest.raises(ProbeFailure) as captured:
        await probe.observe()
    assert captured.value.code == code
    assert str(captured.value) == code
    assert calls == 1


class Chunks(httpx2.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for start in range(0, len(self.body), 1024):
            yield self.body[start : start + 1024]


@pytest.mark.asyncio
async def test_chunked_response_cannot_exceed_bound() -> None:
    probe = Probe(
        settings(),
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, stream=Chunks(b"x" * (MAX_RESPONSE_BYTES + 1)))
        ),
    )
    with pytest.raises(ProbeFailure, match="probe_response_too_large"):
        await probe.observe()


@pytest.mark.asyncio
async def test_5xx_requires_no_json_body() -> None:
    probe = Probe(
        settings(),
        transport=httpx2.MockTransport(lambda request: httpx2.Response(503, content=b"upstream")),
        clock=lambda: NOW,
    )
    observed = await probe.observe()
    assert observed.http_status == 503
    assert observed.value == 1.0
    assert observed.order_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "request_timeout", "total_timeout"])
async def test_network_and_total_timeout_are_bounded_without_retry(failure: str) -> None:
    calls = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if failure == "network":
            raise httpx2.ConnectError("secret token and hostname", request=request)
        if failure == "request_timeout":
            raise httpx2.ReadTimeout("private remote detail", request=request)
        await asyncio.sleep(0.1)
        return httpx2.Response(200, content=GOOD_BODY)

    probe = Probe(settings(request_timeout_seconds=0.01), transport=httpx2.MockTransport(handler))
    expected = "probe_unavailable" if failure == "network" else "probe_timeout"
    with pytest.raises(ProbeFailure) as captured:
        await probe.observe()
    assert str(captured.value) == expected
    assert calls == 1


def test_constructor_revalidates_forged_remote_address_and_timeout() -> None:
    provided = settings()
    for changes in (
        {"fault_lab_base_url": "https://external.example"},
        {"fault_lab_base_url": "http://order-service/private"},
        {"request_timeout_seconds": True},
        {"request_timeout_seconds": float("nan")},
        {"request_timeout_seconds": 11},
    ):
        with pytest.raises(ValueError):
            Probe(provided.model_copy(update=changes))
