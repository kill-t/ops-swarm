"""确定性补查建议编译的领域测试。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.follow_up_planning.policy import compile_follow_up_proposal
from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.review.contracts import FollowUpSuggestion
from opsswarm.investigation.review_routing.contracts import (
    ReviewRoutingDecision,
    ReviewRoutingRecord,
)
from opsswarm.investigation.tasks.errors import TaskFactConflict


def route_record(*, round_number: int = 1) -> ReviewRoutingRecord:
    suggestions = (
        FollowUpSuggestion(
            role=EvidenceKind.METRIC, question="检查等待队列", rationale="排除容量耗尽"
        ),
        FollowUpSuggestion(
            role=EvidenceKind.CHANGE, question="核对近期发布", rationale="排除变更影响"
        ),
        FollowUpSuggestion(
            role=EvidenceKind.LOG, question="检查依赖错误", rationale="定位异常路径"
        ),
    )
    decision = ReviewRoutingDecision(
        route="follow_up",
        reason_codes=(
            "support_insufficient",
            "evidence_gaps_present",
            "follow_up_suggestions_present",
            "follow_up_capacity_available",
        ),
        round_number=round_number,
        existing_task_count=3,
        max_rounds=3,
        max_tasks_per_round=3,
        max_total_tasks=6,
        remaining_rounds=3 - round_number,
        remaining_task_slots=3,
        suggested_task_count=3,
        follow_up_suggestions=suggestions,
    )
    return ReviewRoutingRecord(
        id=uuid4(),
        investigation_run_id=uuid4(),
        round_id=uuid4(),
        open_hypothesis_id=uuid4(),
        hypothesis_review_id=uuid4(),
        investigation_attempt_id=uuid4(),
        version=decision.version,
        status=decision.status,
        review_sha256="1" * 64,
        policy_sha256="2" * 64,
        decision=decision,
        content_sha256=content_hash(decision),
        created_at=datetime(2026, 9, 22, tzinfo=UTC),
    )


def test_compile_follow_up_preserves_all_suggestions_with_system_scope() -> None:
    proposal = compile_follow_up_proposal(route_record())

    assert proposal.round_number == 2
    assert [item.role.value for item in proposal.tasks] == ["change", "log", "metric"]
    assert [item.task_key for item in proposal.tasks] == ["change", "log", "metric"]
    assert [item.goal for item in proposal.tasks] == [
        "核对近期发布",
        "检查依赖错误",
        "检查等待队列",
    ]
    assert [item.lookback_seconds for item in proposal.tasks] == [21600, 900, 900]


def test_compile_follow_up_only_accepts_first_round_follow_up_route() -> None:
    second_round_route = route_record(round_number=2)
    with pytest.raises(TaskFactConflict) as caught:
        compile_follow_up_proposal(second_round_route)
    assert caught.value.code == "follow_up_round_not_supported"

    candidate_decision = ReviewRoutingDecision(
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
    candidate = route_record().model_copy(
        update={
            "decision": candidate_decision,
            "content_sha256": content_hash(candidate_decision),
            "version": candidate_decision.version,
            "status": candidate_decision.status,
        }
    )
    with pytest.raises(TaskFactConflict) as caught:
        compile_follow_up_proposal(candidate)
    assert caught.value.code == "follow_up_route_required"
