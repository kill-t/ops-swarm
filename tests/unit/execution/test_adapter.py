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
from opsswarm.fault_lab.controller import FaultController
from opsswarm.investigation.models import HypothesisStatus, Recommendation, RootCause
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
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
