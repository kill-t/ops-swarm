"""发布回执的严格来源、只读网络和历史/当前对账边界。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx2
import pytest
from pydantic import ValidationError

from opsswarm.execution.release.receipt_client import FaultLabReceiptClient
from opsswarm.execution.release.receipt_contracts import (
    RECEIPT_PATH,
    ReceiptError,
    ReceiptObservation,
)
from opsswarm.execution.release.reconciliation import reconcile_receipt
from opsswarm.execution.settings import ExecutionSettings
from opsswarm.fault_lab.release.contracts import ReleaseCommand
from opsswarm.fault_lab.release.policy import command_hash
from opsswarm.fault_lab.release.receipts import ReleaseReceipt

NOW = datetime(2026, 10, 26, 4, tzinfo=UTC)
KEY = "candidate-release-00000000-0000-0000-0000-000000000125"


def expected(**changes):
    return ReleaseCommand.model_validate(
        dict(
            expected_version="2026.01.01-rc1",
            expected_revision=2,
            target_version="stable",
            idempotency_key=KEY,
            **changes,
        )
    )


def payload(*, missing=False, superseded=False):
    body = expected().model_dump(mode="json")
    stamp = (NOW - timedelta(seconds=2)).isoformat()
    return dict(
        version="lab-release-receipt-v1",
        service_name="order-service",
        source="local-release-simulation-v1",
        simulation=True,
        idempotency_key=KEY,
        status="not_found" if missing else "recorded",
        command=None
        if missing
        else dict(
            id=str(UUID(int=125)),
            service_name="order-service",
            sequence=2,
            kind="cas",
            command=body,
            command_sha256=command_hash(body),
            previous_version="2026.01.01-rc1",
            previous_revision=2,
            version="stable",
            revision=3,
            created_at=stamp,
        ),
        current=dict(
            service_name="order-service",
            source="local-release-simulation-v1",
            simulation=True,
            version="2026.01.01-rc1" if superseded else "stable",
            revision=4 if superseded else 3,
            command_count=3 if superseded else 2,
            release_500_enabled=superseded,
            updated_at=NOW.isoformat() if superseded else stamp,
        ),
        effect_current=not missing and not superseded,
    )


def observation(raw=None, *, observed_at=NOW):
    return ReceiptObservation.model_validate(
        dict(
            receipt=payload() if raw is None else raw,
            observed_at=observed_at,
            source_path=RECEIPT_PATH + KEY,
        )
    )


def reconcile(raw=None, **kwargs):
    return reconcile_receipt(
        expected(), observation(raw), not_before=NOW - timedelta(seconds=3), now=NOW, **kwargs
    )


@pytest.mark.parametrize(
    "missing,superseded,status",
    [
        (False, False, "matched_current"),
        (False, True, "matched_superseded"),
        (True, False, "not_recorded"),
    ],
)
def test_exact_history_and_current_effect_are_separate(missing, superseded, status):
    assert reconcile(payload(missing=missing, superseded=superseded)).status == status


def test_later_noop_keeps_effect_but_aba_loses_it():
    raw = payload()
    raw["current"].update(command_count=3, updated_at=NOW.isoformat())
    assert reconcile(raw).status == "matched_current"
    raw["current"].update(command_count=4, revision=5)
    raw["effect_current"] = False
    assert reconcile(raw).status == "matched_superseded"


@pytest.mark.parametrize(
    "path,value",
    [
        (("simulation",), 1),
        (("current", "simulation"), 1),
        (("effect_current",), 1),
        (("source",), "deployment"),
        (("service_name",), "other"),
        (("current", "source"), "deployment"),
        (("current", "service_name"), "other"),
        (("version",), "lab-release-receipt-v2"),
        (("status",), "not_found"),
        (("command", "sequence"), True),
        (("command", "command", "expected_revision"), 2.0),
        (("command", "command_sha256"), "0" * 64),
        (("command", "created_at"), 123),
        (("current", "updated_at"), "2026-10-26T04:00:00"),
        (("current", "updated_at"), "0001-01-01T00:00:00+01:00"),
        (("current", "command_count"), 1),
        (("current", "revision"), 4),
        (("current", "version"), "2026.01.01-rc1"),
        (("effect_current",), False),
        (("command", "created_at"), (NOW + timedelta(seconds=1)).isoformat()),
        (("command", "extra"), False),
        (("extra",), False),
    ],
)
def test_invalid_shape_identity_hash_and_state_are_rejected(path, value):
    raw = payload()
    target = raw
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        ReleaseReceipt.model_validate(raw)


@pytest.mark.parametrize(
    "field",
    [
        "version",
        "service_name",
        "source",
        "simulation",
        "idempotency_key",
        "status",
        "command",
        "current",
        "effect_current",
    ],
)
def test_all_envelope_fields_required(field):
    raw = payload()
    raw.pop(field)
    with pytest.raises(ValidationError):
        ReleaseReceipt.model_validate(raw)


@pytest.mark.parametrize("field", ["service_name", "source", "simulation", "updated_at"])
def test_nested_identity_defaults_cannot_fill_missing_remote_material(field):
    raw = payload()
    raw["current"].pop(field)
    with pytest.raises(ValidationError):
        ReleaseReceipt.model_validate(raw)


def test_copied_dtos_and_nonexistent_status_do_not_bypass_validation():
    receipt = ReleaseReceipt.model_validate(payload())
    with pytest.raises(ValidationError):
        observation(receipt.model_copy(update={"simulation": 1}))
    raw = payload(missing=True)
    raw["effect_current"] = True
    with pytest.raises(ValidationError):
        observation(raw)
    with pytest.raises(ValidationError):
        observation(observed_at=NOW - timedelta(seconds=3))


def test_expected_command_or_start_time_conflict_is_not_success():
    material = observation()
    for command in (
        expected().model_copy(update={"idempotency_key": "different-key-125"}),
        expected().model_copy(update={"expected_revision": 4}),
        expected().model_copy(update={"target_version": "2026.01.01-rc1"}),
    ):
        assert (
            reconcile_receipt(
                command, material, not_before=NOW - timedelta(seconds=3), now=NOW
            ).status
            == "conflict"
        )
    assert (
        reconcile_receipt(
            expected(), material, not_before=NOW - timedelta(seconds=1), now=NOW
        ).status
        == "conflict"
    )
    broken = material.model_copy(update={"source_path": "/other"})
    with pytest.raises(ReceiptError):
        reconcile_receipt(expected(), broken, not_before=NOW - timedelta(seconds=3), now=NOW)


@pytest.mark.parametrize("delta", [-1, 30, 31])
def test_future_or_expired_observation_cannot_be_reconciled(delta):
    with pytest.raises(ReceiptError):
        reconcile_receipt(
            expected(),
            observation(),
            not_before=NOW - timedelta(seconds=3),
            now=NOW + timedelta(seconds=delta),
        )


def test_window_just_before_deadline_and_timezone_normalization():
    material = observation(observed_at="2026-10-26T12:00:00+08:00")
    assert material.observed_at == NOW
    assert (
        reconcile_receipt(
            expected(),
            material,
            not_before=NOW - timedelta(seconds=3),
            now=NOW + timedelta(seconds=29, microseconds=999999),
        ).status
        == "matched_current"
    )


def client(handler, **kwargs):
    return FaultLabReceiptClient(
        transport=httpx2.MockTransport(handler), clock=lambda: NOW, **kwargs
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_client_only_gets_exact_key_and_recognizes_explicit_404(missing):
    calls = []

    def handler(request):
        calls.append((request.method, str(request.url), request.content))
        return httpx2.Response(404 if missing else 200, json=payload(missing=missing))

    result = await client(handler).get(KEY)
    assert result.receipt.command is None if missing else result.receipt.command is not None
    assert calls == [("GET", "http://127.0.0.1:18001" + RECEIPT_PATH + KEY, b"")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,raw",
    [
        (404, {"detail": "Not Found"}),
        (404, payload()),
        (200, payload(missing=True)),
        (201, payload()),
        (503, {"secret": "token"}),
        (307, payload()),
    ],
)
async def test_generic_or_wrong_status_never_means_not_recorded(status, raw):
    with pytest.raises(ReceiptError) as caught:
        await client(lambda _: httpx2.Response(status, json=raw)).get(KEY)
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_wrong_key_duplicate_json_and_invalid_key_fail_without_writing():
    raw = payload()
    raw["idempotency_key"] = "another-valid-key"
    with pytest.raises(ReceiptError):
        await client(lambda _: httpx2.Response(200, json=raw)).get(KEY)
    duplicate = json.dumps(payload()).replace(
        '"simulation": true', '"simulation": false,"simulation": true', 1
    )
    with pytest.raises(ReceiptError):
        await client(lambda _: httpx2.Response(200, content=duplicate)).get(KEY)
    calls = []
    for key in ("../invalid-key", "key?query=1", " key-padding", "x" * 101):
        with pytest.raises(ReceiptError):
            await client(lambda req: calls.append(req)).get(key)
    assert calls == []


@pytest.mark.asyncio
async def test_timeout_covers_transport_and_client_configuration_is_revalidated():
    entered = asyncio.Event()

    async def handler(_):
        entered.set()
        await asyncio.Event().wait()

    with pytest.raises(ReceiptError, match="超时"):
        await asyncio.wait_for(
            client(handler, settings=ExecutionSettings(request_timeout_seconds=0.01)).get(KEY), 1
        )
    assert entered.is_set()
    with pytest.raises(ValueError):
        FaultLabReceiptClient(
            ExecutionSettings().model_copy(update={"fault_lab_base_url": "https://evil.invalid"})
        )
