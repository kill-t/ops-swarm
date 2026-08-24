"""故障控制器单元测试。"""

from opsswarm.fault_lab.controller import FaultController


def test_release_500_is_disabled_by_default() -> None:
    controller = FaultController()

    assert controller.snapshot().release_500_enabled is False


def test_release_500_can_be_enabled_and_disabled() -> None:
    controller = FaultController()

    enabled = controller.set_release_500(enabled=True)
    disabled = controller.set_release_500(enabled=False)

    assert enabled.release_500_enabled is True
    assert disabled.release_500_enabled is False
