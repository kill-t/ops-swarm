"""故障控制器单元测试。"""

import pytest

from opsswarm.fault_lab.controller import (
    DatabasePoolValueConflict,
    FaultController,
    RedisAvailabilityConflict,
)


def test_release_500_is_disabled_by_default() -> None:
    controller = FaultController()

    assert controller.snapshot().release_500_enabled is False


def test_database_pool_uses_safe_default_capacity() -> None:
    snapshot = FaultController().snapshot()

    assert snapshot.database_pool_size == 50
    assert snapshot.database_pool_saturation == 0.2
    assert snapshot.database_pool_waiters == 0
    assert snapshot.database_pool_saturated is False


def test_redis_dependency_is_available_by_default() -> None:
    snapshot = FaultController().snapshot()

    assert snapshot.redis_available is True
    assert snapshot.redis_unavailable_ratio == 0.0


def test_release_500_can_be_enabled_and_disabled() -> None:
    controller = FaultController()

    enabled = controller.set_release_500(enabled=True)
    disabled = controller.set_release_500(enabled=False)

    assert enabled.release_500_enabled is True
    assert disabled.release_500_enabled is False


def test_database_pool_capacity_deterministically_controls_saturation() -> None:
    controller = FaultController()

    saturated = controller.set_database_pool_size(value=5)
    recovered = controller.set_database_pool_size(value=50)

    assert saturated.database_pool_saturation == 1.0
    assert saturated.database_pool_waiters == 5
    assert saturated.database_pool_saturated is True
    assert saturated.database_pool_timeout_enabled is True
    assert recovered.database_pool_saturation == 0.2
    assert recovered.database_pool_waiters == 0
    assert recovered.database_pool_saturated is False
    assert recovered.database_pool_timeout_enabled is False


def test_full_pool_is_saturated_but_does_not_yet_have_waiters() -> None:
    snapshot = FaultController(database_pool_size=10).snapshot()

    assert snapshot.database_pool_saturation == 1.0
    assert snapshot.database_pool_saturated is True
    assert snapshot.database_pool_waiters == 0
    assert snapshot.database_pool_timeout_enabled is False


def test_database_pool_compare_and_set_rejects_drift_without_overwriting() -> None:
    controller = FaultController(database_pool_size=7)

    with pytest.raises(DatabasePoolValueConflict) as captured:
        controller.set_database_pool_size(value=50, expected_value=5)

    assert captured.value.expected_value == 5
    assert captured.value.actual_value == 7
    assert controller.snapshot().database_pool_size == 7


def test_database_pool_compare_and_set_updates_matching_value() -> None:
    controller = FaultController(database_pool_size=5)

    updated = controller.set_database_pool_size(value=50, expected_value=5)

    assert updated.database_pool_size == 50


def test_redis_compare_and_set_restores_matching_unavailable_state() -> None:
    controller = FaultController(redis_available=False)

    restored = controller.set_redis_available(
        available=True,
        expected_available=False,
    )

    assert restored.redis_available is True
    assert restored.redis_unavailable_ratio == 0.0


def test_redis_compare_and_set_rejects_drift_without_overwriting() -> None:
    controller = FaultController(redis_available=True)

    with pytest.raises(RedisAvailabilityConflict) as captured:
        controller.set_redis_available(available=True, expected_available=False)

    assert captured.value.expected_available is False
    assert captured.value.actual_available is True
    assert controller.snapshot().redis_available is True


@pytest.mark.parametrize("value", [0, 1001, True])
def test_database_pool_rejects_non_whitelisted_values(value: int) -> None:
    controller = FaultController()

    with pytest.raises(ValueError, match="1～1000"):
        controller.set_database_pool_size(value=value)


@pytest.mark.parametrize("value", [0, 1, "true"])
def test_redis_rejects_non_boolean_values(value: object) -> None:
    controller = FaultController()

    with pytest.raises(ValueError, match="布尔值"):
        controller.set_redis_available(available=value)  # type: ignore[arg-type]
