"""独立审查保持有界意见、证据闭包与非执行边界。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.context.prompts import (
    HYPOTHESIS_REVIEW_PROMPT,
    THIRD_ROUND_HYPOTHESIS_REVIEW_PROMPT,
)
from opsswarm.investigation.models import InvestigationRequest
from opsswarm.investigation.review.contracts import (
    HypothesisReview,
    HypothesisReviewProposal,
    HypothesisReviewRecord,
)
from opsswarm.investigation.review.policy import validate_review_evidence
from opsswarm.investigation.review.service import _RoundHypothesisReviewer
from opsswarm.investigation.synthesis.contracts import (
    OpenHypothesis,
    OpenHypothesisProposal,
    OpenHypothesisRecord,
)
from opsswarm.investigation.tasks.errors import TaskFactConflict


def hypothesis() -> OpenHypothesis:
    return OpenHypothesis.from_proposal(
        OpenHypothesisProposal(
            statement="数据库连接池耗尽可能解释当前异常",
            confidence=0.65,
            supporting_evidence_ids=("log-1", "metric-1"),
            counter_evidence_ids=("change-1",),
            unresolved_questions=("连接池上限是否发生过变更？",),
        )
    )


def hypothesis_record() -> OpenHypothesisRecord:
    value = hypothesis()
    return OpenHypothesisRecord(
        id=uuid4(),
        investigation_run_id=uuid4(),
        round_id=uuid4(),
        investigation_attempt_id=uuid4(),
        source_call_id=uuid4(),
        version=value.version,
        status=value.status,
        hypothesis=value,
        content_sha256=content_hash(value),
        created_at=datetime.now(UTC),
    )


def proposal(**changes: object) -> HypothesisReviewProposal:
    values: dict[str, object] = {
        "support_is_sufficient": True,
        "rationale": "日志和指标在同一时间窗内相互印证。",
        "accepted_supporting_evidence_ids": ("log-1", "metric-1"),
        "counter_evidence_ids": ("change-1",),
        "evidence_gaps": (),
        "competing_explanations": ("下游依赖抖动",),
        "suggested_direction": "evaluate_for_routing",
        "follow_up_suggestions": (),
    }
    values.update(changes)
    return HypothesisReviewProposal.model_validate(values)


def test_system_records_review_without_routing_or_execution_fields() -> None:
    review = HypothesisReview.from_proposal(proposal())

    assert review.version == "hypothesis-review-v1"
    assert review.status == "recorded"
    assert "route" not in HypothesisReview.model_fields
    assert "action" not in HypothesisReview.model_fields
    assert "approved" not in HypothesisReview.model_fields


def test_review_service_uses_explicit_third_round_prompt() -> None:
    request = InvestigationRequest(
        incident_id=uuid4(),
        service_name="checkout-api",
        title="错误率升高",
        started_at=datetime.now(UTC),
    )

    _, first_messages = _RoundHypothesisReviewer._supplied_input(
        request,
        open_hypothesis=hypothesis_record(),
        specialist_results=(),
        round_number=1,
    )
    _, third_messages = _RoundHypothesisReviewer._supplied_input(
        request,
        open_hypothesis=hypothesis_record(),
        specialist_results=(),
        round_number=3,
    )

    assert first_messages[0].content == HYPOTHESIS_REVIEW_PROMPT
    assert third_messages[0].content == THIRD_ROUND_HYPOTHESIS_REVIEW_PROMPT


def test_review_service_rejects_unknown_round() -> None:
    request = InvestigationRequest(
        incident_id=uuid4(),
        service_name="checkout-api",
        title="错误率升高",
        started_at=datetime.now(UTC),
    )

    with pytest.raises(TaskFactConflict) as caught:
        _RoundHypothesisReviewer._supplied_input(
            request,
            open_hypothesis=hypothesis_record(),
            specialist_results=(),
            round_number=4,
        )
    assert caught.value.code == "hypothesis_review_scope_invalid"


@pytest.mark.parametrize(
    "changes",
    [
        {"accepted_supporting_evidence_ids": ("log-1", "log-1")},
        {"counter_evidence_ids": ("log-1",)},
        {"competing_explanations": ("依赖抖动", "依赖抖动")},
        {
            "suggested_direction": "follow_up",
            "support_is_sufficient": False,
            "accepted_supporting_evidence_ids": (),
            "evidence_gaps": ("缺少连接池快照",),
            "follow_up_suggestions": (
                {"role": "metric", "question": "补查连接数", "rationale": "确认是否打满"},
                {"role": "metric", "question": "补查等待数", "rationale": "确认排队情况"},
            ),
        },
    ],
)
def test_review_rejects_duplicate_or_overlapping_material(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        proposal(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {
            "suggested_direction": "evaluate_for_routing",
            "support_is_sufficient": False,
            "accepted_supporting_evidence_ids": (),
        },
        {
            "suggested_direction": "follow_up",
            "support_is_sufficient": False,
            "accepted_supporting_evidence_ids": (),
            "counter_evidence_ids": (),
            "evidence_gaps": (),
            "competing_explanations": (),
            "follow_up_suggestions": (),
        },
        {
            "suggested_direction": "human_review",
            "support_is_sufficient": True,
        },
    ],
)
def test_review_direction_requires_consistent_bounded_opinion(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        proposal(**changes)


def test_follow_up_is_a_limited_suggestion_without_task_identity() -> None:
    review = proposal(
        suggested_direction="follow_up",
        support_is_sufficient=False,
        accepted_supporting_evidence_ids=(),
        evidence_gaps=("缺少连接池等待队列指标",),
        follow_up_suggestions=(
            {"role": "metric", "question": "补查等待队列", "rationale": "区分耗尽与慢查询"},
        ),
    )

    suggestion = review.follow_up_suggestions[0]
    assert suggestion.role.value == "metric"
    assert "task_id" not in type(suggestion).model_fields
    assert "service_name" not in type(suggestion).model_fields
    assert "max_attempts" not in type(suggestion).model_fields


def test_review_policy_rejects_unknown_or_unproposed_supporting_evidence() -> None:
    with pytest.raises(TaskFactConflict) as outside_hypothesis:
        validate_review_evidence(
            proposal(accepted_supporting_evidence_ids=("metric-2",)),
            hypothesis(),
            {"log-1", "metric-1", "metric-2", "change-1"},
        )
    assert outside_hypothesis.value.code == "hypothesis_review_support_mismatch"

    with pytest.raises(TaskFactConflict) as outside_context:
        validate_review_evidence(
            proposal(counter_evidence_ids=("change-2",)),
            hypothesis(),
            {"log-1", "metric-1", "change-1"},
        )
    assert outside_context.value.code == "hypothesis_review_evidence_mismatch"


def test_review_record_rejects_tampered_content_hash() -> None:
    review = HypothesisReview.from_proposal(proposal())
    values = {
        "id": uuid4(),
        "investigation_run_id": uuid4(),
        "round_id": uuid4(),
        "open_hypothesis_id": uuid4(),
        "investigation_attempt_id": uuid4(),
        "source_call_id": uuid4(),
        "version": review.version,
        "status": review.status,
        "review": review,
        "content_sha256": content_hash(review),
        "created_at": datetime.now(UTC),
    }
    assert HypothesisReviewRecord.model_validate(values).review == review

    with pytest.raises(ValidationError):
        HypothesisReviewRecord.model_validate({**values, "content_sha256": "0" * 64})
