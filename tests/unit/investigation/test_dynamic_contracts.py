"""动态图跨轮结果契约测试。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.coordination.contracts import (
    CompiledCoordinationPlan,
    CoordinationPlanRecord,
    CoordinationProposal,
    ProposedTask,
)
from opsswarm.investigation.coordination.hashing import coordination_hash
from opsswarm.investigation.dynamic_contracts import FollowUpExecutionResult
from opsswarm.investigation.models import EvidenceKind, SpecialistReport
from opsswarm.investigation.review.contracts import (
    FollowUpSuggestion,
    HypothesisReview,
    HypothesisReviewRecord,
)
from opsswarm.investigation.review_routing.contracts import (
    ReviewRoutingDecision,
    ReviewRoutingRecord,
)
from opsswarm.investigation.synthesis.contracts import OpenHypothesis, OpenHypothesisRecord
from opsswarm.investigation.tasks.contracts import RoundAssignment, TaskAssignment
from opsswarm.investigation.tasks.results import SpecialistTaskResult

NOW = datetime(2026, 9, 28, tzinfo=UTC)
SNAPSHOT_ID = "scenario-contract"


def _task(role: EvidenceKind) -> TaskAssignment:
    return TaskAssignment(
        task_key=role.value,
        role=role,
        goal=f"调查 {role.value}",
        service_name="checkout",
        window_start=NOW - timedelta(minutes=15),
        window_end=NOW,
        evidence_snapshot_id=SNAPSHOT_ID,
    )


def _plan(
    run_id: UUID,
    round_id: UUID,
    *,
    round_number: int,
    roles: tuple[EvidenceKind, ...],
    source_call_id: UUID | None = None,
    source_review_route_id: UUID | None = None,
) -> CoordinationPlanRecord:
    assignments = tuple(_task(role) for role in roles)
    proposal = CoordinationProposal(
        round_number=round_number,
        goal=f"第 {round_number} 轮",
        tasks=tuple(
            ProposedTask(
                task_key=task.task_key,
                role=task.role,
                goal=task.goal,
                lookback_seconds=900,
            )
            for task in assignments
        ),
    )
    compiled = CompiledCoordinationPlan(
        policy_version="dynamic-coordination-v1",
        proposal=proposal,
        assignment=RoundAssignment(
            round_number=round_number,
            goal=proposal.goal,
            tasks=assignments,
        ),
    )
    return CoordinationPlanRecord(
        round_id=round_id,
        investigation_run_id=run_id,
        investigation_attempt_id=uuid4(),
        source_call_id=source_call_id,
        source_review_route_id=source_review_route_id,
        version=compiled.version,
        policy_sha256="1" * 64,
        plan=compiled,
        content_sha256=coordination_hash(compiled),
        created_at=NOW,
    )


def _result(role: EvidenceKind, *, snapshot_id: str = SNAPSHOT_ID) -> SpecialistTaskResult:
    return SpecialistTaskResult(
        evidence_snapshot_id=snapshot_id,
        report=SpecialistReport(
            specialist=role,
            summary=f"{role.value} 结果",
            findings=("发现",),
            confidence=0.7,
        ),
        evidence=(),
    )


def _valid_payload() -> dict[str, object]:
    run_id, first_round_id = uuid4(), uuid4()
    first_plan = _plan(
        run_id,
        first_round_id,
        round_number=1,
        roles=(EvidenceKind.LOG,),
        source_call_id=uuid4(),
    )
    hypothesis = OpenHypothesis(
        statement="依赖容量不足",
        confidence=0.5,
        supporting_evidence_ids=("log-1",),
        unresolved_questions=("等待队列是否饱和",),
    )
    hypothesis_record = OpenHypothesisRecord(
        id=uuid4(),
        investigation_run_id=run_id,
        round_id=first_round_id,
        investigation_attempt_id=uuid4(),
        source_call_id=uuid4(),
        version=hypothesis.version,
        status=hypothesis.status,
        hypothesis=hypothesis,
        content_sha256=content_hash(hypothesis),
        created_at=NOW,
    )
    suggestions = (
        FollowUpSuggestion(
            role=EvidenceKind.METRIC,
            question="检查等待队列",
            rationale="排除容量耗尽",
        ),
        FollowUpSuggestion(
            role=EvidenceKind.CHANGE,
            question="核对近期变更",
            rationale="排除发布影响",
        ),
    )
    review = HypothesisReview(
        support_is_sufficient=False,
        rationale="仍有证据缺口",
        accepted_supporting_evidence_ids=("log-1",),
        evidence_gaps=("缺少指标和变更证据",),
        suggested_direction="follow_up",
        follow_up_suggestions=suggestions,
    )
    review_record = HypothesisReviewRecord(
        id=uuid4(),
        investigation_run_id=run_id,
        round_id=first_round_id,
        open_hypothesis_id=hypothesis_record.id,
        investigation_attempt_id=uuid4(),
        source_call_id=uuid4(),
        version=review.version,
        status=review.status,
        review=review,
        content_sha256=content_hash(review),
        created_at=NOW,
    )
    decision = ReviewRoutingDecision(
        route="follow_up",
        reason_codes=(
            "support_insufficient",
            "evidence_gaps_present",
            "follow_up_suggestions_present",
            "follow_up_capacity_available",
        ),
        round_number=1,
        existing_task_count=1,
        max_rounds=3,
        max_tasks_per_round=3,
        max_total_tasks=6,
        remaining_rounds=2,
        remaining_task_slots=5,
        suggested_task_count=2,
        follow_up_suggestions=suggestions,
    )
    route = ReviewRoutingRecord(
        id=uuid4(),
        investigation_run_id=run_id,
        round_id=first_round_id,
        open_hypothesis_id=hypothesis_record.id,
        hypothesis_review_id=review_record.id,
        investigation_attempt_id=uuid4(),
        version=decision.version,
        status=decision.status,
        review_sha256=review_record.content_sha256,
        policy_sha256="1" * 64,
        decision=decision,
        content_sha256=content_hash(decision),
        created_at=NOW,
    )
    follow_up_plan = _plan(
        run_id,
        uuid4(),
        round_number=2,
        roles=(EvidenceKind.CHANGE, EvidenceKind.METRIC),
        source_review_route_id=route.id,
    )
    return {
        "coordination_plan": first_plan,
        "specialist_results": (_result(EvidenceKind.LOG),),
        "open_hypothesis": hypothesis_record,
        "hypothesis_review": review_record,
        "review_route": route,
        "follow_up_plan": follow_up_plan,
        "follow_up_results": (
            _result(EvidenceKind.METRIC),
            _result(EvidenceKind.CHANGE),
        ),
    }


def test_follow_up_execution_accepts_complete_results_independent_of_order() -> None:
    result = FollowUpExecutionResult(**_valid_payload())

    assert [item.report.specialist for item in result.follow_up_results] == [
        EvidenceKind.METRIC,
        EvidenceKind.CHANGE,
    ]


@pytest.mark.parametrize(
    "follow_up_results",
    [
        (),
        (_result(EvidenceKind.METRIC),),
        (_result(EvidenceKind.METRIC), _result(EvidenceKind.METRIC)),
        (_result(EvidenceKind.METRIC), _result(EvidenceKind.CHANGE, snapshot_id="other")),
    ],
)
def test_follow_up_execution_rejects_incomplete_duplicate_or_wrong_snapshot_results(
    follow_up_results: tuple[SpecialistTaskResult, ...],
) -> None:
    payload = _valid_payload()
    payload["follow_up_results"] = follow_up_results

    with pytest.raises(ValidationError, match="第二轮专业结果"):
        FollowUpExecutionResult(**payload)


def test_non_follow_up_route_requires_empty_second_round_results() -> None:
    payload = _valid_payload()
    decision = ReviewRoutingDecision(
        route="candidate_evaluation",
        reason_codes=("support_sufficient", "candidate_requirements_satisfied"),
        round_number=1,
        existing_task_count=1,
        max_rounds=3,
        max_tasks_per_round=3,
        max_total_tasks=6,
        remaining_rounds=2,
        remaining_task_slots=5,
        suggested_task_count=0,
    )
    route = payload["review_route"]
    assert isinstance(route, ReviewRoutingRecord)
    payload["review_route"] = route.model_copy(
        update={
            "decision": decision,
            "version": decision.version,
            "status": decision.status,
            "content_sha256": content_hash(decision),
        }
    )
    payload["follow_up_plan"] = None
    payload["follow_up_results"] = ()

    assert FollowUpExecutionResult(**payload).follow_up_results == ()

    payload["follow_up_results"] = (_result(EvidenceKind.METRIC),)
    with pytest.raises(ValidationError, match="非补查路由不能携带第二轮专业结果"):
        FollowUpExecutionResult(**payload)
