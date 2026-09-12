"""动态协调提案不能扩大服务、角色、时间窗或任务额度。"""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from opsswarm.investigation.coordination.contracts import (
    CoordinationPolicy,
    CoordinationProposal,
    ProposedTask,
)
from opsswarm.investigation.coordination.policy import compile_coordination_plan
from opsswarm.investigation.models import EvidenceKind, InvestigationRequest
from opsswarm.investigation.tasks.errors import TaskFactConflict
from tests.task_facts_helpers import BASE_TIME


def request() -> InvestigationRequest:
    return InvestigationRequest(
        incident_id="11111111-1111-1111-1111-111111111111",
        service_name="order-service",
        title="订单接口异常",
        started_at=BASE_TIME - timedelta(minutes=5),
    )


def proposal(*tasks: ProposedTask, round_number: int = 1) -> CoordinationProposal:
    return CoordinationProposal(
        round_number=round_number,
        goal="核对最相关的专业材料",
        tasks=tasks
        or (
            ProposedTask(
                task_key="log",
                role=EvidenceKind.LOG,
                goal="检查异常日志模式",
                lookback_seconds=600,
            ),
            ProposedTask(
                task_key="change",
                role=EvidenceKind.CHANGE,
                goal="排除无关发布",
                lookback_seconds=3600,
            ),
        ),
    )


def compile(proposed: CoordinationProposal, **changes):
    return compile_coordination_plan(
        proposed,
        policy=changes.pop("policy", CoordinationPolicy()),
        request=request(),
        expected_round_number=changes.pop("expected_round_number", 1),
        existing_task_count=changes.pop("existing_task_count", 0),
        observation_cutoff_at=changes.pop("observation_cutoff_at", BASE_TIME),
        evidence_snapshot_id=changes.pop("evidence_snapshot_id", "run-snapshot-v1"),
        **changes,
    )


def test_compiler_injects_scope_and_preserves_dynamic_choice():
    plan = compile(proposal())
    assert plan.policy_version == "dynamic-coordination-v1"
    assert [task.role for task in plan.assignment.tasks] == [
        EvidenceKind.LOG,
        EvidenceKind.CHANGE,
    ]
    for task in plan.assignment.tasks:
        proposed = next(item for item in plan.proposal.tasks if item.role is task.role)
        assert task.service_name == "order-service"
        assert task.window_end == BASE_TIME
        assert (task.window_end - task.window_start).total_seconds() == proposed.lookback_seconds
        assert task.evidence_snapshot_id == "run-snapshot-v1"
        assert task.max_attempts == 3


@pytest.mark.parametrize(
    "tasks",
    [
        (
            ProposedTask(task_key="log", role="log", goal="一", lookback_seconds=60),
            ProposedTask(task_key="log-2", role="log", goal="二", lookback_seconds=60),
        ),
        (
            ProposedTask(task_key="log", role="log", goal="一", lookback_seconds=60),
            ProposedTask(task_key="log", role="metric", goal="二", lookback_seconds=60),
        ),
    ],
)
def test_proposal_rejects_duplicate_role_or_key(tasks):
    with pytest.raises(ValidationError):
        proposal(*tasks)


@pytest.mark.parametrize(
    ("proposed", "changes", "code"),
    [
        (proposal(round_number=2), {}, "coordination_round_invalid"),
        (proposal(), {"existing_task_count": 5}, "coordination_task_budget_exceeded"),
        (
            proposal(
                ProposedTask(
                    task_key="metric-check", role="metric", goal="指标", lookback_seconds=60
                )
            ),
            {},
            "coordination_task_key_invalid",
        ),
        (
            proposal(ProposedTask(task_key="log", role="log", goal="日志", lookback_seconds=901)),
            {},
            "coordination_window_invalid",
        ),
        (
            proposal(),
            {"observation_cutoff_at": BASE_TIME - timedelta(hours=1)},
            "coordination_window_invalid",
        ),
        (
            proposal(),
            {"observation_cutoff_at": datetime(2026, 9, 5)},
            "coordination_window_invalid",
        ),
        (proposal(), {"evidence_snapshot_id": ""}, "coordination_snapshot_invalid"),
    ],
)
def test_compiler_rejects_scope_or_budget_expansion(proposed, changes, code):
    with pytest.raises(TaskFactConflict) as caught:
        compile(proposed, **changes)
    assert caught.value.code == code


def test_policy_and_proposal_reject_unknown_or_unbounded_values():
    with pytest.raises(ValidationError):
        CoordinationPolicy(max_rounds=4)
    with pytest.raises(ValidationError):
        CoordinationProposal.model_validate(
            {
                **proposal().model_dump(mode="json"),
                "shell": "anything",
            }
        )


def test_compilation_is_deterministic_for_equivalent_timezone():
    first = compile(proposal())
    second = compile(
        proposal(),
        observation_cutoff_at=BASE_TIME.astimezone(timezone(timedelta(hours=8))),
    )
    assert first == second
