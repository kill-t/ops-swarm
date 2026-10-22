"""从原交接的真实成功审查与人工事件前缀重建准备来源。"""

from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy
from opsswarm.investigation.human_prepare.contracts import PreparationError, PreparationSource
from opsswarm.investigation.human_wait.state import load_wait_state
from opsswarm.investigation.human_wait.validation import project_wait_state
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.review.contracts import HypothesisReviewRecord
from opsswarm.investigation.review.persistence import validated_review_call
from opsswarm.investigation.review.tables import InvestigationHypothesisReviewRow
from opsswarm.investigation.review_routing.contracts import ReviewRoutingRecord
from opsswarm.investigation.review_routing.tables import InvestigationHypothesisReviewRouteRow


async def load_preparation_source(
    session: AsyncSession, run: InvestigationRunRow, *, wait_version: int
) -> PreparationSource:
    handoff, current = await load_wait_state(session, run.id)
    if not 2 <= wait_version <= 16 or len(current.events) < wait_version - 1:
        raise PreparationError("human_preparation_source_conflict", "准备缺少完整人工补充前缀")
    prefix = current.events[: wait_version - 1]
    if any(event.action != "supplement" for event in prefix):
        raise PreparationError("human_preparation_source_conflict", "取消不能成为准备输入")
    route_row = await session.get(
        InvestigationHypothesisReviewRouteRow, handoff.source_review_route_id
    )
    assert route_row is not None  # 上面的完整交接核验已经验证该来源链。
    route = ReviewRoutingRecord.model_validate(route_row, from_attributes=True)
    review_row = await session.get(InvestigationHypothesisReviewRow, route.hypothesis_review_id)
    assert review_row is not None
    review = HypothesisReviewRecord.model_validate(review_row, from_attributes=True)
    prior_input, _, prior_manifest = await validated_review_call(
        session, run, review.source_call_id, expected_round_number=route.decision.round_number
    )
    if run.started_at is None or run.call_budget is None or run.model_budget is None:
        raise PreparationError("human_preparation_source_conflict", "原运行缺少固定预算来源")
    return PreparationSource(
        handoff=handoff,
        wait=project_wait_state(handoff, prefix),
        route=route,
        review=review,
        prior_input=prior_input,
        prior_manifest=prior_manifest,
        call_budget=CallBudgetPolicy.model_validate(run.call_budget),
        model_budget=ModelBudgetPolicy.model_validate(run.model_budget),
        started_at=run.started_at,
    )
