"""发布任务客户端的精确窗口、完整材料与单次有界网络。"""

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx2
import pytest

from opsswarm.execution.release_jobs.client import (
    DISPATCH_PATH,
    MAX_RESPONSE_BYTES,
    DispatchClientError,
    DispatchObservation,
    ReleaseDispatchClient,
    validate_observation,
)
from opsswarm.execution.settings import ExecutionSettings
from opsswarm.fault_lab.release.dispatch_contracts import DispatchCommand
from opsswarm.fault_lab.release.policy import command_hash

NOW = datetime(2026, 10, 26, 2, tzinfo=UTC)
OPERATION = UUID("01020304-0102-0304-0506-010203040506")
COMMAND_ID = "01020304-0102-0304-0506-010203040507"


def command():
    return DispatchCommand(
        operation_id=OPERATION,
        command={
            "expected_version": "2026.01.01-rc1",
            "expected_revision": 2,
            "target_version": "stable",
            "idempotency_key": f"candidate-release-{OPERATION}",
        },
        not_before=NOW - timedelta(seconds=5),
        expires_at=NOW + timedelta(seconds=15),
    )


def view(status="prepared", *, expected=None, checked=NOW, superseded=False):
    original = expected or command()
    applied = status == "applied"
    created = original.not_before + timedelta(seconds=1)
    finished = created + timedelta(seconds=1)
    current = {
        "service_name": "order-service",
        "source": "local-release-simulation-v1",
        "simulation": True,
        "version": "stable" if applied else "2026.01.01-rc1",
        "revision": 3 if applied else 2,
        "command_count": 2 if applied else 1,
        "release_500_enabled": not applied,
        "updated_at": (finished if applied else original.not_before).isoformat(),
    }
    if superseded:
        current.update(
            version="2026.01.01-rc1",
            revision=4,
            command_count=3,
            release_500_enabled=True,
            updated_at=NOW.isoformat(),
        )
    cas = original.command.model_dump(mode="json")
    digest = command_hash(cas)
    record = {
        **original.model_dump(mode="json"),
        "policy_version": "lab-release-dispatch-v1",
        "command_sha256": digest,
        "status": status,
        "command_id": COMMAND_ID if applied else None,
        "created_at": created.isoformat(),
        "finished_at": finished.isoformat() if status != "prepared" else None,
    }
    receipt = {
        "version": "lab-release-receipt-v1",
        "service_name": "order-service",
        "source": "local-release-simulation-v1",
        "simulation": True,
        "idempotency_key": original.command.idempotency_key,
        "status": "recorded" if applied else "not_found",
        "command": (
            {
                "id": COMMAND_ID,
                "service_name": "order-service",
                "sequence": 2,
                "kind": "cas",
                "command": cas,
                "command_sha256": digest,
                "previous_version": "2026.01.01-rc1",
                "previous_revision": 2,
                "version": "stable",
                "revision": 3,
                "created_at": finished.isoformat(),
            }
            if applied
            else None
        ),
        "current": current,
        "effect_current": applied and not superseded,
    }
    return {
        "record": record,
        "availability": (
            status
            if status != "prepared"
            else "expired"
            if checked >= original.expires_at
            else "ready"
        ),
        "checked_at": checked.isoformat(),
        "receipt": receipt,
    }


def client(handler, **kwargs):
    return ReleaseDispatchClient(
        transport=httpx2.MockTransport(handler), clock=lambda: NOW, **kwargs
    )


@pytest.mark.asyncio
async def test_register_execute_lookup_use_fixed_paths_original_json_and_single_requests():
    calls = []

    def handler(request):
        calls.append((request.method, str(request.url), request.content))
        if request.method == "GET":
            return httpx2.Response(200, json=view("applied"))
        return httpx2.Response(
            201,
            json={
                "applied": True,
                "dispatch": view("applied" if request.method == "PUT" else "prepared"),
            },
        )

    remote = client(handler)
    prepared = await remote.register(command())
    executed = await remote.execute(command())
    observed = await remote.lookup(command())
    assert prepared.source_path == DISPATCH_PATH
    assert executed.view.record.status == observed.view.record.status == "applied"
    assert [item[:2] for item in calls] == [
        ("POST", "http://127.0.0.1:18001" + DISPATCH_PATH),
        ("PUT", f"http://127.0.0.1:18001{DISPATCH_PATH}/{OPERATION}"),
        ("GET", f"http://127.0.0.1:18001{DISPATCH_PATH}/{OPERATION}"),
    ]
    assert json.loads(calls[0][2]) == command().model_dump(mode="json")
    assert calls[1][2] == calls[2][2] == b""
    assert DispatchObservation.model_validate(observed.model_dump(mode="json")) == observed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["prepared", "applied", "cancelled"])
async def test_lookup_accepts_complete_lifecycle_and_historical_effect(status):
    payload = view(status, superseded=status == "applied")
    result = await client(lambda _: httpx2.Response(200, json=payload)).lookup(command())
    assert result.view.record.status == status
    if status == "applied":
        assert result.view.receipt.effect_current is False


@pytest.mark.asyncio
async def test_expired_window_is_readable_but_does_not_authorize_another_write():
    original = command().model_copy(
        update={"not_before": NOW - timedelta(seconds=20), "expires_at": NOW - timedelta(seconds=1)}
    )
    result = await client(lambda _: httpx2.Response(200, json=view(expected=original))).lookup(
        original
    )
    assert result.view.availability == "expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["register", "execute"])
async def test_precise_200_write_replay_response_is_accepted(method):
    result = await getattr(
        client(
            lambda _: httpx2.Response(200, json={"applied": False, "dispatch": view("applied")})
        ),
        method,
    )(command())
    assert result.view.record.status == "applied"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["register", "execute", "lookup"])
@pytest.mark.parametrize("status", [301, 307, 404, 409, 500, 503])
async def test_http_failures_never_retry_follow_redirect_or_turn_404_into_success(method, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(
            status, headers={"location": "https://private.invalid"}, text="secret"
        )

    with pytest.raises(DispatchClientError) as failure:
        await getattr(client(handler), method)(command())
    assert failure.value.code == "release_dispatch_unavailable"
    assert len(calls) == 1 and "secret" not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("applied", [1, "true", None, False])
async def test_write_envelope_requires_real_boolean_and_matching_http_status(applied):
    with pytest.raises(DispatchClientError):
        await client(
            lambda _: httpx2.Response(201, json={"applied": applied, "dispatch": view()})
        ).register(command())


@pytest.mark.asyncio
async def test_execute_does_not_accept_prepared_response_as_execution():
    with pytest.raises(DispatchClientError):
        await client(
            lambda _: httpx2.Response(201, json={"applied": True, "dispatch": view()})
        ).execute(command())


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("record", "operation_id"), "01020304-0102-0304-0506-010203040599"),
        (("record", "command_sha256"), "0" * 64),
        (("record", "created_at"), (NOW - timedelta(seconds=6)).isoformat()),
        (("record", "created_at"), 1792980000),
        (("record", "finished_at"), NOW.isoformat()),
        (("record", "command_id"), "01020304-0102-0304-0506-010203040599"),
        (("record", "expires_at"), (NOW + timedelta(seconds=16)).isoformat()),
        (("record", "command", "expected_revision"), True),
        (("availability",), "ready"),
        (("checked_at",), (NOW + timedelta(seconds=1)).isoformat()),
        (("checked_at",), (NOW - timedelta(seconds=31)).isoformat()),
        (("checked_at",), "2026-10-26T02:00:00"),
        (("checked_at",), 1792980000),
        (("receipt", "simulation"), 1),
        (("receipt", "source"), "production"),
        (("receipt", "idempotency_key"), "another-key"),
        (("receipt", "effect_current"), 1),
        (("receipt", "current", "simulation"), 1),
        (("receipt", "current", "service_name"), "other-service"),
        (("receipt", "current", "updated_at"), (NOW + timedelta(seconds=1)).isoformat()),
        (("receipt", "command", "id"), "01020304-0102-0304-0506-010203040599"),
        (("receipt", "command", "created_at"), NOW.isoformat()),
    ],
)
def test_cross_field_tampering_cannot_be_reused_for_persistent_result(path, value):
    payload = copy.deepcopy(view("applied"))
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        observation = DispatchObservation(view=payload, observed_at=NOW, source_path=DISPATCH_PATH)
        validate_observation(command(), observation, now=NOW)


@pytest.mark.parametrize("field", ["service_name", "source", "simulation"])
def test_missing_current_identity_is_not_filled_with_release_state_defaults(field):
    payload = view()
    payload["receipt"]["current"].pop(field)
    with pytest.raises(ValueError):
        DispatchObservation(view=payload, observed_at=NOW, source_path=DISPATCH_PATH)


@pytest.mark.parametrize(
    ("source", "observed", "now"),
    [
        ("/__admin/simulated-release", NOW, NOW),
        (DISPATCH_PATH + "/other", NOW, NOW),
        (DISPATCH_PATH, NOW, NOW - timedelta(seconds=1)),
        (DISPATCH_PATH, NOW, NOW + timedelta(seconds=30)),
    ],
)
def test_wrong_path_future_or_stale_material_is_rejected(source, observed, now):
    with pytest.raises(ValueError):
        observation = DispatchObservation(view=view(), observed_at=observed, source_path=source)
        validate_observation(command(), observation, now=now)


def test_persisted_material_revalidation_catches_unvalidated_model_copy():
    original = DispatchObservation(view=view(), observed_at=NOW, source_path=DISPATCH_PATH)
    forged = original.model_copy(update={"source_path": "/other"})
    with pytest.raises(DispatchClientError):
        validate_observation(command(), forged, now=NOW)


def test_complete_valid_material_from_another_cas_revision_is_not_original_operation():
    original = command()
    another = original.model_copy(
        update={"command": original.command.model_copy(update={"expected_revision": 4})}
    )
    observation = DispatchObservation(
        view=view(expected=another), observed_at=NOW, source_path=DISPATCH_PATH
    )
    with pytest.raises(DispatchClientError):
        validate_observation(original, observation, now=NOW)


def test_applied_command_at_exact_window_expiry_is_rejected():
    original = command().model_copy(update={"expires_at": NOW})
    payload = view("applied", expected=original)
    payload["record"]["finished_at"] = NOW.isoformat()
    payload["receipt"]["command"]["created_at"] = NOW.isoformat()
    payload["receipt"]["current"]["updated_at"] = NOW.isoformat()
    with pytest.raises(ValueError):
        DispatchObservation(view=payload, observed_at=NOW, source_path=DISPATCH_PATH)


def test_cancelled_material_can_be_read_after_original_window_expired():
    original = command().model_copy(
        update={
            "not_before": NOW - timedelta(seconds=20),
            "expires_at": NOW - timedelta(seconds=1),
        }
    )
    payload = view("cancelled", expected=original)
    payload["record"]["finished_at"] = NOW.isoformat()
    observation = DispatchObservation(view=payload, observed_at=NOW, source_path=DISPATCH_PATH)
    assert validate_observation(original, observation, now=NOW).view.availability == "cancelled"


def test_strict_iso_offsets_are_normalized_to_utc():
    payload = view()
    payload["checked_at"] = "2026-10-26T10:00:00+08:00"
    result = DispatchObservation(
        view=payload, observed_at="2026-10-26T10:00:00+08:00", source_path=DISPATCH_PATH
    )
    assert result.observed_at == result.view.checked_at == NOW
    assert result.observed_at.tzinfo is result.view.checked_at.tzinfo is UTC


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
    with pytest.raises(DispatchClientError):
        await client(lambda _: httpx2.Response(200, stream=stream)).lookup(command())
    assert stream.reads == MAX_RESPONSE_BYTES // 8192 + 1
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"content-length": "65537"},
        {"content-length": "-1"},
        {"content-length": "invalid"},
        {"content-encoding": "gzip"},
    ],
)
async def test_invalid_headers_reject_before_reading_body(headers):
    stream = Chunks([b"private"])
    with pytest.raises(DispatchClientError):
        await client(lambda _: httpx2.Response(200, headers=headers, stream=stream)).lookup(
            command()
        )
    assert stream.reads == 0 and stream.closed


@pytest.mark.asyncio
async def test_exact_byte_limit_is_accepted_without_truncation():
    payload = json.dumps(view()).encode()
    payload += b" " * (MAX_RESPONSE_BYTES - len(payload))
    result = await client(lambda _: httpx2.Response(200, content=payload)).lookup(command())
    assert result.view.record.operation_id == OPERATION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [b'{"record":null,"record":null}', b'{"record":NaN}', b"{} trailing"]
)
async def test_duplicate_json_nonfinite_or_trailing_bytes_are_rejected(payload):
    with pytest.raises(DispatchClientError):
        await client(lambda _: httpx2.Response(200, content=payload)).lookup(command())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx2.ConnectError, httpx2.ReadTimeout])
async def test_network_error_is_single_attempt_and_private_details_remain_private(failure):
    calls = []

    def handler(request):
        calls.append(request)
        raise failure("credential=secret", request=request)

    with pytest.raises(DispatchClientError) as caught:
        await client(handler).register(command())
    assert len(calls) == 1 and "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_total_timeout_bounds_transport_even_when_it_ignores_httpx_timeout():
    entered = asyncio.Event()

    async def handler(_):
        entered.set()
        await asyncio.Event().wait()

    with pytest.raises(DispatchClientError) as failure:
        await asyncio.wait_for(
            client(handler, settings=ExecutionSettings(request_timeout_seconds=0.01)).register(
                command()
            ),
            timeout=1,
        )
    assert failure.value.code == "release_dispatch_timeout" and entered.is_set()


@pytest.mark.asyncio
async def test_total_timeout_bounds_body_and_closes_stream():
    class Slow(httpx2.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"{"
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    stream = Slow()
    with pytest.raises(DispatchClientError) as failure:
        await asyncio.wait_for(
            client(
                lambda _: httpx2.Response(200, stream=stream),
                settings=ExecutionSettings(request_timeout_seconds=0.01),
            ).execute(command()),
            timeout=1,
        )
    assert failure.value.code == "release_dispatch_timeout" and stream.closed


@pytest.mark.parametrize(
    "changes", [{"fault_lab_base_url": "https://remote.invalid"}, {"request_timeout_seconds": 11}]
)
def test_model_copy_cannot_bypass_settings_guards(changes):
    with pytest.raises(ValueError):
        ReleaseDispatchClient(ExecutionSettings().model_copy(update=changes))


@pytest.mark.asyncio
async def test_environment_proxy_redirects_and_compression_are_disabled(monkeypatch):
    original = httpx2.AsyncClient
    observed = []

    def wrap(**kwargs):
        observed.append((kwargs["trust_env"], kwargs["follow_redirects"], kwargs["headers"]))
        return original(**kwargs)

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setattr(httpx2, "AsyncClient", wrap)
    await client(lambda _: httpx2.Response(200, json=view())).lookup(command())
    assert observed == [(False, False, {"Accept-Encoding": "identity"})]
