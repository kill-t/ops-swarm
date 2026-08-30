"""本地故障实验室白名单适配器测试。"""

from datetime import UTC, datetime
from uuid import UUID

import httpx2
import pytest
from pydantic import ValidationError

from opsswarm.execution import (
    ExecutionSettings,
    FaultLabOperationAdapter,
    OperationAdapterFailure,
    OperationResult,
)
from opsswarm.fault_lab.app import create_app
from opsswarm.fault_lab.controller import FaultController, FaultSnapshot
from opsswarm.investigation.models import HypothesisStatus, Recommendation, RootCause
from opsswarm.investigation.scenarios import (
    DATABASE_POOL_SCENARIO_V2,
    REDIS_UNAVAILABLE_SCENARIO_V2,
    RELEASE_500_SCENARIO,
)
from opsswarm.planning import propose_action_plan
from opsswarm.planning.persistence import StoredActionStep


def rollback_step() -> StoredActionStep:
    plan = propose_action_plan(
        service_name="order-service",
        root_cause=RootCause.RELEASE_500_REGRESSION,
        recommendation=Recommendation.ROLLBACK_RELEASE,
        hypothesis_status=HypothesisStatus.SUPPORTED,
        evidence_items=RELEASE_500_SCENARIO.evidence,
    )
    return StoredActionStep(
        id=UUID("00000000-0000-0000-0000-000000000301"),
        created_at=datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        **plan.steps[0].model_dump(),
    )


def configuration_rollback_step() -> StoredActionStep:
    plan = propose_action_plan(
        service_name="order-service",
        root_cause=RootCause.DATABASE_POOL_MISCONFIGURATION,
        recommendation=Recommendation.ROLLBACK_CONFIGURATION,
        hypothesis_status=HypothesisStatus.SUPPORTED,
        evidence_items=DATABASE_POOL_SCENARIO_V2.evidence,
    )
    return StoredActionStep(
        id=UUID("00000000-0000-0000-0000-000000000302"),
        created_at=datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        **plan.steps[0].model_dump(),
    )


def redis_restore_step() -> StoredActionStep:
    plan = propose_action_plan(
        service_name="order-service",
        root_cause=RootCause.REDIS_UNAVAILABLE,
        recommendation=Recommendation.RESTORE_DEPENDENCY,
        hypothesis_status=HypothesisStatus.SUPPORTED,
        evidence_items=REDIS_UNAVAILABLE_SCENARIO_V2.evidence,
    )
    return StoredActionStep(
        id=UUID("00000000-0000-0000-0000-000000000303"),
        created_at=datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        **plan.steps[0].model_dump(),
    )


@pytest.mark.asyncio
async def test_adapter_only_disables_fixed_local_fault() -> None:
    controller = FaultController()
    controller.set_release_500(enabled=True)
    transport = httpx2.ASGITransport(app=create_app(controller))
    adapter = FaultLabOperationAdapter(
        ExecutionSettings(fault_lab_base_url="http://order-service"),
        transport=transport,
    )

    result = await adapter.execute(rollback_step())

    assert controller.snapshot().release_500_enabled is False
    assert result.output == {
        "fault": "release_500",
        "enabled": False,
        "service": "order-service",
        "target_version": "stable",
    }


@pytest.mark.asyncio
async def test_adapter_rejects_target_outside_whitelist_before_http() -> None:
    adapter = FaultLabOperationAdapter(
        ExecutionSettings(fault_lab_base_url="http://127.0.0.1:18001")
    )
    unsafe_step = rollback_step().model_copy(update={"target_name": "payment-service"})

    with pytest.raises(OperationAdapterFailure, match="只允许") as captured:
        await adapter.execute(unsafe_step)

    assert captured.value.error_code == "operation_not_whitelisted"


@pytest.mark.asyncio
async def test_adapter_rolls_back_database_pool_with_compare_and_set() -> None:
    controller = FaultController(database_pool_size=5)
    adapter = FaultLabOperationAdapter(
        ExecutionSettings(fault_lab_base_url="http://order-service"),
        transport=httpx2.ASGITransport(app=create_app(controller)),
    )

    result = await adapter.execute(configuration_rollback_step())

    assert controller.snapshot().database_pool_size == 50
    assert result.output == {
        "configuration_key": "database_pool_size",
        "planned_current_value": 5,
        "target_value": 50,
        "current_value": 50,
        "database_pool_saturation": 0.2,
        "already_at_target": False,
        "service": "order-service",
    }


@pytest.mark.asyncio
async def test_adapter_rejects_configuration_drift_without_overwriting() -> None:
    controller = FaultController(database_pool_size=7)
    adapter = FaultLabOperationAdapter(
        ExecutionSettings(fault_lab_base_url="http://order-service"),
        transport=httpx2.ASGITransport(app=create_app(controller)),
    )

    with pytest.raises(OperationAdapterFailure, match="当前值为 7") as captured:
        await adapter.execute(configuration_rollback_step())

    assert captured.value.error_code == "configuration_current_value_mismatch"
    assert controller.snapshot().database_pool_size == 7


@pytest.mark.asyncio
async def test_adapter_rejects_configuration_changed_between_read_and_write() -> None:
    class DriftingController(FaultController):
        def __init__(self) -> None:
            super().__init__(database_pool_size=5)
            self._drifted = False

        def snapshot(self) -> FaultSnapshot:
            snapshot = super().snapshot()
            if not self._drifted:
                self._drifted = True
                self.set_database_pool_size(value=7)
            return snapshot

    controller = DriftingController()
    adapter = FaultLabOperationAdapter(
        ExecutionSettings(fault_lab_base_url="http://order-service"),
        transport=httpx2.ASGITransport(app=create_app(controller)),
    )

    with pytest.raises(OperationAdapterFailure, match="当前值为 7") as captured:
        await adapter.execute(configuration_rollback_step())

    assert captured.value.error_code == "configuration_current_value_mismatch"
    assert controller.snapshot().database_pool_size == 7


@pytest.mark.asyncio
async def test_adapter_treats_configuration_already_at_target_as_safe_replay() -> None:
    controller = FaultController(database_pool_size=50)
    adapter = FaultLabOperationAdapter(
        ExecutionSettings(fault_lab_base_url="http://order-service"),
        transport=httpx2.ASGITransport(app=create_app(controller)),
    )

    result = await adapter.execute(configuration_rollback_step())

    assert result.output["already_at_target"] is True
    assert result.output["current_value"] == 50


@pytest.mark.asyncio
async def test_adapter_restores_redis_with_compare_and_set() -> None:
    controller = FaultController(redis_available=False)
    adapter = FaultLabOperationAdapter(
        ExecutionSettings(fault_lab_base_url="http://order-service"),
        transport=httpx2.ASGITransport(app=create_app(controller)),
    )

    result = await adapter.execute(redis_restore_step())

    assert controller.snapshot().redis_available is True
    assert result.output == {
        "dependency": "redis",
        "expected_available": False,
        "target_available": True,
        "available": True,
        "redis_unavailable_ratio": 0.0,
        "already_at_target": False,
    }


@pytest.mark.asyncio
async def test_adapter_treats_redis_already_available_as_safe_replay() -> None:
    controller = FaultController(redis_available=True)
    adapter = FaultLabOperationAdapter(
        ExecutionSettings(fault_lab_base_url="http://order-service"),
        transport=httpx2.ASGITransport(app=create_app(controller)),
    )

    result = await adapter.execute(redis_restore_step())

    assert result.output["already_at_target"] is True
    assert result.output["available"] is True


@pytest.mark.asyncio
async def test_adapter_rejects_redis_changed_between_read_and_write() -> None:
    class DriftingRedisController(FaultController):
        def __init__(self) -> None:
            super().__init__(redis_available=False)
            self._drifted = False

        def snapshot(self) -> FaultSnapshot:
            snapshot = super().snapshot()
            if not self._drifted:
                self._drifted = True
                self.set_redis_available(available=True)
            return snapshot

    controller = DriftingRedisController()
    adapter = FaultLabOperationAdapter(
        ExecutionSettings(fault_lab_base_url="http://order-service"),
        transport=httpx2.ASGITransport(app=create_app(controller)),
    )

    with pytest.raises(OperationAdapterFailure, match="计划审批状态") as captured:
        await adapter.execute(redis_restore_step())

    assert captured.value.error_code == "dependency_current_state_mismatch"
    assert controller.snapshot().redis_available is True


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:18001",
        "http://example.com",
        "http://127.0.0.1:18001/admin",
        "http://user:secret@127.0.0.1:18001",
    ],
)
def test_settings_rejects_non_local_or_composed_url(url: str) -> None:
    with pytest.raises(ValidationError):
        ExecutionSettings(fault_lab_base_url=url)


def test_operation_result_rejects_unbounded_output() -> None:
    with pytest.raises(ValidationError, match="16 KiB"):
        OperationResult(output={"payload": "x" * (16 * 1024)})
