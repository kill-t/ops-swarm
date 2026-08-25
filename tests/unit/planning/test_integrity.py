"""审批计划内容摘要的稳定性测试。"""

from uuid import UUID

from opsswarm.investigation.models import HypothesisStatus, RootCause
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
from opsswarm.planning import ActionPlanStatus, action_plan_content_hash, propose_action_plan

PLAN_ID = UUID("00000000-0000-0000-0000-000000000101")
RUN_ID = UUID("00000000-0000-0000-0000-000000000102")
HYPOTHESIS_ID = UUID("00000000-0000-0000-0000-000000000103")


def test_action_plan_hash_is_stable_and_ignores_lifecycle_status() -> None:
    plan = propose_action_plan(
        service_name="order-service",
        root_cause=RootCause.RELEASE_500_REGRESSION,
        hypothesis_status=HypothesisStatus.SUPPORTED,
        evidence_items=RELEASE_500_SCENARIO.evidence,
    )
    submitted = plan.model_copy(update={"status": ActionPlanStatus.AWAITING_APPROVAL})

    draft_hash = action_plan_content_hash(
        plan,
        plan_id=PLAN_ID,
        investigation_run_id=RUN_ID,
        hypothesis_id=HYPOTHESIS_ID,
    )
    submitted_hash = action_plan_content_hash(
        submitted,
        plan_id=PLAN_ID,
        investigation_run_id=RUN_ID,
        hypothesis_id=HYPOTHESIS_ID,
    )

    assert draft_hash == submitted_hash
    assert len(draft_hash) == 64


def test_action_plan_hash_changes_when_approved_parameters_change() -> None:
    plan = propose_action_plan(
        service_name="order-service",
        root_cause=RootCause.RELEASE_500_REGRESSION,
        hypothesis_status=HypothesisStatus.SUPPORTED,
        evidence_items=RELEASE_500_SCENARIO.evidence,
    )
    step = plan.steps[0]
    changed_step = step.model_copy(
        update={"parameters": step.parameters.model_copy(update={"target_version": "stable-v2"})}
    )
    changed = plan.model_copy(update={"steps": (changed_step,)})

    original_hash = action_plan_content_hash(
        plan,
        plan_id=PLAN_ID,
        investigation_run_id=RUN_ID,
        hypothesis_id=HYPOTHESIS_ID,
    )
    changed_hash = action_plan_content_hash(
        changed,
        plan_id=PLAN_ID,
        investigation_run_id=RUN_ID,
        hypothesis_id=HYPOTHESIS_ID,
    )

    assert original_hash != changed_hash
