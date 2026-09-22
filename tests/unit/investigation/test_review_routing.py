"""确定性审查路由保留异议、容量和完整补查建议。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.coordination.contracts import CoordinationPolicy
from opsswarm.investigation.review.contracts import HypothesisReview, HypothesisReviewProposal
from opsswarm.investigation.review_routing.contracts import ReviewRoutingRecord
from opsswarm.investigation.review_routing.policy import compile_review_routing_decision
from opsswarm.investigation.synthesis.contracts import OpenHypothesis, OpenHypothesisProposal
from opsswarm.investigation.tasks.errors import TaskFactConflict


def hypothesis(*, counter_evidence_ids: tuple[str, ...] = ()) -> OpenHypothesis:
    return OpenHypothesis.from_proposal(
        OpenHypothesisProposal(
            statement="数据库连接池耗尽可能解释当前异常",
            confidence=0.75,
            supporting_evidence_ids=("log-1", "metric-1"),
            counter_evidence_ids=counter_evidence_ids,
            unresolved_questions=("故障是否会在高峰期复现？",),
        )
    )


def sufficient_review(**changes: object) -> HypothesisReview:
    values: dict[str, object] = {
        "support_is_sufficient": True,
        "rationale": "日志和指标在同一时间窗内相互印证。",
        "accepted_supporting_evidence_ids": ("log-1", "metric-1"),
        "counter_evidence_ids": (),
        "evidence_gaps": (),
        "competing_explanations": (),
        "suggested_direction": "evaluate_for_routing",
        "follow_up_suggestions": (),
    }
    values.update(changes)
    return HypothesisReview.from_proposal(HypothesisReviewProposal.model_validate(values))


def insufficient_review(*, roles: tuple[str, ...] = ("metric",)) -> HypothesisReview:
    suggestions = tuple(
        {
            "role": role,
            "question": f"补查 {role} 证据",
            "rationale": "确认连接池是否耗尽",
        }
        for role in roles
    )
    return HypothesisReview.from_proposal(
        HypothesisReviewProposal.model_validate(
            {
                "support_is_sufficient": False,
                "rationale": "当前材料仍缺少关键观测。",
                "accepted_supporting_evidence_ids": (),
                "counter_evidence_ids": (),
                "evidence_gaps": ("缺少连接池等待队列指标",),
                "competing_explanations": (),
                "suggested_direction": "follow_up" if suggestions else "human_review",
                "follow_up_suggestions": suggestions,
            }
        )
    )


def compile_decision(
    review: HypothesisReview,
    *,
    current_hypothesis: OpenHypothesis | None = None,
    policy: CoordinationPolicy | None = None,
    round_number: int = 1,
    existing_task_count: int = 2,
):
    return compile_review_routing_decision(
        current_hypothesis or hypothesis(),
        review,
        policy=policy or CoordinationPolicy(),
        round_number=round_number,
        existing_task_count=existing_task_count,
    )


def test_clean_sufficient_review_enters_candidate_evaluation() -> None:
    decision = compile_decision(sufficient_review())

    assert decision.route == "candidate_evaluation"
    assert decision.reason_codes == (
        "support_sufficient",
        "candidate_requirements_satisfied",
    )
    assert decision.remaining_rounds == 2
    assert decision.remaining_task_slots == 4
    assert decision.follow_up_suggestions == ()


@pytest.mark.parametrize(
    ("current_hypothesis", "review", "reason"),
    [
        (
            hypothesis(counter_evidence_ids=("change-1",)),
            sufficient_review(),
            "hypothesis_counter_evidence_present",
        ),
        (
            hypothesis(),
            sufficient_review(counter_evidence_ids=("change-1",)),
            "review_counter_evidence_present",
        ),
        (
            hypothesis(),
            sufficient_review(evidence_gaps=("缺少连接池上限",)),
            "evidence_gaps_present",
        ),
        (
            hypothesis(),
            sufficient_review(competing_explanations=("下游依赖抖动",)),
            "competing_explanations_present",
        ),
    ],
)
def test_sufficient_review_with_material_objection_waits_for_human(
    current_hypothesis: OpenHypothesis,
    review: HypothesisReview,
    reason: str,
) -> None:
    decision = compile_decision(review, current_hypothesis=current_hypothesis)

    assert decision.route == "await_human"
    assert reason in decision.reason_codes
    assert "follow_up_suggestions_missing" in decision.reason_codes


def test_insufficient_review_with_capacity_preserves_all_suggestions() -> None:
    review = insufficient_review(roles=("log", "metric"))

    decision = compile_decision(review, existing_task_count=3)

    assert decision.route == "follow_up"
    assert decision.suggested_task_count == 2
    assert decision.follow_up_suggestions == review.follow_up_suggestions
    assert decision.reason_codes == (
        "support_insufficient",
        "evidence_gaps_present",
        "follow_up_suggestions_present",
        "follow_up_capacity_available",
    )


@pytest.mark.parametrize(
    ("policy", "round_number", "existing_task_count", "reason"),
    [
        (CoordinationPolicy(max_rounds=1), 1, 1, "round_limit_reached"),
        (
            CoordinationPolicy(max_tasks_per_round=1),
            1,
            1,
            "per_round_task_limit_exceeded",
        ),
        (CoordinationPolicy(max_total_tasks=3), 1, 2, "total_task_limit_exceeded"),
    ],
)
def test_follow_up_capacity_limit_waits_for_human_without_dropping_suggestions(
    policy: CoordinationPolicy,
    round_number: int,
    existing_task_count: int,
    reason: str,
) -> None:
    review = insufficient_review(roles=("log", "metric"))

    decision = compile_decision(
        review,
        policy=policy,
        round_number=round_number,
        existing_task_count=existing_task_count,
    )

    assert decision.route == "await_human"
    assert reason in decision.reason_codes
    assert decision.follow_up_suggestions == review.follow_up_suggestions


def test_insufficient_review_without_follow_up_waits_for_human() -> None:
    decision = compile_decision(insufficient_review(roles=()))

    assert decision.route == "await_human"
    assert "follow_up_suggestions_missing" in decision.reason_codes


def test_compiler_rejects_invalid_progress_or_review_source() -> None:
    with pytest.raises(TaskFactConflict) as invalid_round:
        compile_decision(
            sufficient_review(), round_number=3, policy=CoordinationPolicy(max_rounds=2)
        )
    assert invalid_round.value.code == "review_routing_round_invalid"

    with pytest.raises(TaskFactConflict) as invalid_count:
        compile_decision(
            sufficient_review(),
            existing_task_count=4,
            policy=CoordinationPolicy(max_total_tasks=3),
        )
    assert invalid_count.value.code == "review_routing_task_count_invalid"

    with pytest.raises(TaskFactConflict) as invalid_source:
        compile_decision(
            sufficient_review(accepted_supporting_evidence_ids=("metric-2",)),
        )
    assert invalid_source.value.code == "review_routing_source_invalid"


def test_decision_rejects_noncanonical_reasons_or_inconsistent_counts() -> None:
    decision = compile_decision(sufficient_review())

    with pytest.raises(ValidationError):
        type(decision).model_validate(
            {**decision.model_dump(), "reason_codes": tuple(reversed(decision.reason_codes))}
        )
    with pytest.raises(ValidationError):
        type(decision).model_validate({**decision.model_dump(), "remaining_task_slots": 3})


def test_route_record_rejects_tampered_content_hash() -> None:
    decision = compile_decision(sufficient_review())
    values = {
        "id": uuid4(),
        "investigation_run_id": uuid4(),
        "round_id": uuid4(),
        "open_hypothesis_id": uuid4(),
        "hypothesis_review_id": uuid4(),
        "investigation_attempt_id": uuid4(),
        "version": decision.version,
        "status": decision.status,
        "review_sha256": "1" * 64,
        "policy_sha256": "2" * 64,
        "decision": decision,
        "content_sha256": content_hash(decision),
        "created_at": datetime.now(UTC),
    }
    assert ReviewRoutingRecord.model_validate(values).decision == decision

    with pytest.raises(ValidationError):
        ReviewRoutingRecord.model_validate({**values, "content_sha256": "0" * 64})
