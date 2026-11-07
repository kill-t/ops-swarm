import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from opsswarm.verification.candidate import worker as worker_module
from opsswarm.verification.candidate.contracts import ProbeObservation, RecoveryClaim, RecoveryError
from opsswarm.verification.candidate.probe import Probe, ProbeFailure
from opsswarm.verification.candidate.store import CandidateRecoveryStore
from opsswarm.verification.candidate.worker import CandidateRecoveryWorker


def setup_worker(decisions):
    now = datetime.now(UTC)
    claim = RecoveryClaim(
        job_id=uuid4(),
        run_id=uuid4(),
        attempt_id=uuid4(),
        number=1,
        lease_token=uuid4(),
        lease_expires_at=now + timedelta(seconds=30),
    )
    store = SimpleNamespace(
        claim_next=AsyncMock(return_value=claim),
        authorize_sample=AsyncMock(side_effect=decisions),
        save_sample=AsyncMock(),
        complete=AsyncMock(),
    )
    probe = SimpleNamespace(
        observe=AsyncMock(
            return_value=ProbeObservation(
                requested_at=now,
                observed_at=now,
                http_status=200,
                value=0.0,
                order_id="opsswarm-verification-probe",
            )
        )
    )
    sleep = AsyncMock()
    runner = CandidateRecoveryWorker(
        cast(CandidateRecoveryStore, store),
        probe=cast(Probe, probe),
        worker_id="recovery-test",
        sleep=sleep,
    )
    return runner, claim, store, probe, sleep


@pytest.mark.asyncio
async def test_early_sample_waits_without_new_attempt_or_early_probe():
    runner, claim, store, probe, sleep = setup_worker(
        [1, RecoveryError("recovery_sample_too_early", "间隔不足"), 2, 3]
    )

    assert await runner.run_once()

    store.claim_next.assert_awaited_once()
    assert store.authorize_sample.await_count == 4
    assert all(call.args == (claim,) for call in store.authorize_sample.await_args_list)
    assert probe.observe.await_count == 3
    assert [call.args[:2] for call in store.save_sample.await_args_list] == [
        (claim, 1),
        (claim, 2),
        (claim, 3),
    ]
    assert [call.args for call in sleep.await_args_list] == [(2,), (2,), (2,)]
    store.complete.assert_awaited_once_with(claim)


@pytest.mark.asyncio
async def test_lease_loss_during_wait_preserves_saved_sample_and_stops():
    runner, _, store, probe, _ = setup_worker(
        [
            1,
            RecoveryError("recovery_sample_too_early", "间隔不足"),
            RecoveryError("recovery_lease_lost", "原租约失效"),
        ]
    )

    assert await runner.run_once()

    store.claim_next.assert_awaited_once()
    probe.observe.assert_awaited_once()
    store.save_sample.assert_awaited_once()
    store.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_monotonic_deadline_bounds_repeated_early_authorization(monkeypatch):
    runner, _, store, probe, sleep = setup_worker([])
    store.authorize_sample.side_effect = RecoveryError("recovery_sample_too_early", "间隔不足")
    monkeypatch.setattr(worker_module, "RECOVERY_LEASE_SECONDS", 0.02)

    async def yield_control(seconds):
        await asyncio.sleep(0.002)

    sleep.side_effect = yield_control

    assert await runner.run_once()

    assert store.authorize_sample.await_count >= 1
    store.claim_next.assert_awaited_once()
    probe.observe.assert_not_awaited()
    store.save_sample.assert_not_awaited()
    store.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_samples_share_one_waiting_deadline(monkeypatch):
    runner, _, store, probe, sleep = setup_worker([1, 2, 3])
    monkeypatch.setattr(worker_module, "RECOVERY_LEASE_SECONDS", 0.02)

    async def consume_remaining_time(seconds):
        await asyncio.sleep(0.03)

    sleep.side_effect = consume_remaining_time

    assert await runner.run_once()

    store.authorize_sample.assert_awaited_once()
    probe.observe.assert_awaited_once()
    store.save_sample.assert_awaited_once()
    store.complete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [RecoveryError("recovery_integrity_violation", "历史损坏"), TimeoutError("存储超时")]
)
async def test_other_authorization_errors_are_not_converted_to_waiting(error):
    runner, _, store, probe, _ = setup_worker([error])

    with pytest.raises(type(error)) as caught:
        await runner.run_once()

    assert caught.value is error
    probe.observe.assert_not_awaited()
    store.save_sample.assert_not_awaited()
    store.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_failure_still_persists_only_the_failed_sample():
    runner, claim, store, probe, _ = setup_worker([1])
    probe.observe.side_effect = ProbeFailure("probe_timeout")

    assert await runner.run_once()

    store.save_sample.assert_awaited_once_with(claim, 1, error_code="probe_timeout")
    store.complete.assert_awaited_once_with(claim)
