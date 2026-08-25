"""动作风险策略防篡改测试。"""

import pytest

from opsswarm.investigation.models import HypothesisStatus, RootCause
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
from opsswarm.planning import RiskLevel, propose_action_plan, validate_action_plan_policy


def test_policy_rejects_tampered_low_risk_and_approval_bypass() -> None:
    plan = propose_action_plan(
        service_name="order-service",
        root_cause=RootCause.RELEASE_500_REGRESSION,
        hypothesis_status=HypothesisStatus.SUPPORTED,
        evidence_items=RELEASE_500_SCENARIO.evidence,
    )
    step = plan.steps[0].model_copy(
        update={
            "risk_level": RiskLevel.LOW,
            "requires_human_approval": False,
        }
    )
    tampered = plan.model_copy(
        update={
            "risk_level": RiskLevel.LOW,
            "requires_human_approval": False,
            "steps": (step,),
        }
    )

    with pytest.raises(ValueError, match="风险与确定性策略重算结果不一致"):
        validate_action_plan_policy(tampered)


def test_policy_rejects_unknown_version() -> None:
    plan = propose_action_plan(
        service_name="order-service",
        root_cause=RootCause.RELEASE_500_REGRESSION,
        hypothesis_status=HypothesisStatus.SUPPORTED,
        evidence_items=RELEASE_500_SCENARIO.evidence,
    ).model_copy(update={"risk_policy_version": "action-risk-v0"})

    with pytest.raises(ValueError, match="未知或过期"):
        validate_action_plan_policy(plan)
