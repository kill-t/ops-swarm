"""故障控制器单元测试。"""

import pytest

from opsswarm.fault_lab.controller import DatabasePoolValueConflict, FaultController


def test_release_500_is_disabled_by_default() -> None:
    controller = FaultController()

    assert controller.snapshot().release_500_enabled is False


def test_database_pool_uses_safe_default_capacity() -> None:
    snapshot = FaultController().snapshot()

    assert snapshot.database_pool_size == 50
    assert snapshot.database_pool_saturation == 0.2
    assert snapshot.database_pool_waiters == 0
    assert snapshot.database_pool_saturated is False


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


@pytest.mark.parametrize("value", [0, 1001, True])
def test_database_pool_rejects_non_whitelisted_values(value: int) -> None:
    controller = FaultController()

    with pytest.raises(ValueError, match="1～1000"):
        controller.set_database_pool_size(value=value)
