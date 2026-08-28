"""本地订单恢复探针与规则判定测试。"""

from datetime import UTC, datetime
from uuid import UUID

import httpx2
import pytest
from pydantic import ValidationError

from opsswarm.fault_lab.app import create_app
from opsswarm.fault_lab.controller import FaultController
from opsswarm.investigation.models import HypothesisStatus, Recommendation, RootCause
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
from opsswarm.planning import propose_action_plan
from opsswarm.planning.persistence import StoredActionStep
from opsswarm.verification import (
    FaultLabVerificationAdapter,
    VerificationObservation,
    VerificationProbeFailure,
    VerificationSampleStatus,
    VerificationSettings,
    sample_status,
)


def rollback_step() -> StoredActionStep:
    plan = propose_action_plan(
        service_name="order-service",
        root_cause=RootCause.RELEASE_500_REGRESSION,
        recommendation=Recommendation.ROLLBACK_RELEASE,
        hypothesis_status=HypothesisStatus.SUPPORTED,
        evidence_items=RELEASE_500_SCENARIO.evidence,
    )
    return StoredActionStep(
        id=UUID("00000000-0000-0000-0000-000000000401"),
        created_at=datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        **plan.steps[0].model_dump(),
    )


@pytest.mark.asyncio
async def test_probe_maps_healthy_order_to_zero_5xx_ratio() -> None:
    controller = FaultController()
    adapter = FaultLabVerificationAdapter(
        VerificationSettings(fault_lab_base_url="http://order-service"),
        transport=httpx2.ASGITransport(app=create_app(controller)),
    )

    observation = await adapter.observe(rollback_step())

    assert observation == VerificationObservation(
        signal="http_5xx_ratio",
        value=0.0,
        http_status=200,
    )
    assert sample_status(rollback_step().verification, observation) is (
        VerificationSampleStatus.PASSED
    )


@pytest.mark.asyncio
async def test_probe_maps_fault_response_to_failed_5xx_sample() -> None:
    controller = FaultController()
    controller.set_release_500(enabled=True)
    adapter = FaultLabVerificationAdapter(
        VerificationSettings(fault_lab_base_url="http://order-service"),
        transport=httpx2.ASGITransport(app=create_app(controller)),
    )

    observation = await adapter.observe(rollback_step())

    assert observation.value == 1.0
    assert observation.http_status == 500
    assert sample_status(rollback_step().verification, observation) is (
        VerificationSampleStatus.FAILED
    )


@pytest.mark.asyncio
async def test_probe_rejects_target_outside_whitelist_before_http() -> None:
    adapter = FaultLabVerificationAdapter(
        VerificationSettings(fault_lab_base_url="http://127.0.0.1:18001")
    )
    unsafe_step = rollback_step().model_copy(update={"target_name": "payment-service"})

    with pytest.raises(VerificationProbeFailure, match="只允许") as captured:
        await adapter.observe(unsafe_step)

    assert captured.value.error_code == "verification_not_whitelisted"


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
        VerificationSettings(fault_lab_base_url=url)
