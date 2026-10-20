"""审核纯规则不依赖数据库、HTTP 或外部模型。"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.experience.contracts import ExperienceError, ExperienceSource, canonical_hash
from opsswarm.experience.projection import project_source
from opsswarm.experience.records import CandidateView
from opsswarm.experience.review_contracts import (
    ConfirmedExperienceContent,
    ReviewCommand,
    ReviewView,
)
from opsswarm.experience.review_policy import evaluate_review


@pytest.fixture
def candidate():
    source = ExperienceSource.model_validate_json(
        (Path(__file__).resolve().parents[2] / "fixtures/experience-source-v1.json").read_text(
            encoding="utf-8"
        )
    )
    draft = project_source(source)
    return CandidateView(
        id=uuid4(),
        job_id=uuid4(),
        version=1,
        status="pending_review",
        source_hash=canonical_hash(source),
        content=draft,
        content_hash=canonical_hash(draft),
        created_at=datetime(2026, 10, 20, tzinfo=UTC),
    )


def command(candidate, **overrides):
    return ReviewCommand.model_validate(
        {
            "idempotency_key": "unit-review-001",
            "expected_candidate_version": candidate.version,
            "expected_candidate_content_hash": candidate.content_hash,
            "expected_review_version": 0,
            "decision": "confirm",
            "reason": "核对历史范围与证据",
            **overrides,
        }
    )


def decision(candidate, original):
    return ReviewView(
        id=uuid4(),
        candidate_id=candidate.id,
        candidate_version=candidate.version,
        candidate_content_hash=candidate.content_hash,
        source_hash=candidate.source_hash,
        review_version=1,
        policy_version="experience-review-v1",
        decision=original.decision,
        reviewed_by_id="engineer-1",
        authorization_id=uuid4(),
        idempotency_key=original.idempotency_key,
        command_hash=canonical_hash(original),
        reason=original.reason,
        reviewed_at=candidate.created_at,
    )


@pytest.mark.parametrize("choice", ["confirm", "reject"])
def test_pending_can_receive_one_precise_decision(candidate, choice):
    assert (
        evaluate_review(
            candidate, command(candidate, decision=choice), subject_id="engineer-1", existing=None
        )
        == "apply"
    )


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"expected_candidate_version": 2}, "experience_candidate_version_conflict"),
        ({"expected_candidate_content_hash": "0" * 64}, "experience_candidate_hash_conflict"),
        ({"expected_review_version": 1}, "experience_review_version_conflict"),
    ],
)
def test_pending_rejects_wrong_versions_or_hash(candidate, overrides, code):
    with pytest.raises(ExperienceError) as error:
        evaluate_review(
            candidate, command(candidate, **overrides), subject_id="engineer-1", existing=None
        )
    assert error.value.code == code


def test_same_subject_key_and_normalized_command_replay(candidate):
    original = command(candidate)
    normalized = command(candidate, reason=" 核对历史范围与证据 ")
    assert canonical_hash(original) == canonical_hash(normalized)
    assert (
        evaluate_review(
            candidate, normalized, subject_id="engineer-1", existing=decision(candidate, original)
        )
        == "replay"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason": "另一个理由"},
        {"decision": "reject"},
        {"expected_review_version": 1},
        {"expected_candidate_version": 2},
        {"expected_candidate_content_hash": "0" * 64},
    ],
)
def test_same_key_cannot_change_any_part_of_bound_command(candidate, overrides):
    original = command(candidate)
    with pytest.raises(ExperienceError) as error:
        evaluate_review(
            candidate,
            command(candidate, **overrides),
            subject_id="engineer-1",
            existing=decision(candidate, original),
        )
    assert error.value.code == "experience_review_idempotency_conflict"


@pytest.mark.parametrize(
    ("subject_id", "key"),
    [
        ("engineer-2", "unit-review-001"),
        ("engineer-1", "new-review-command"),
    ],
)
def test_other_subject_or_key_cannot_reverse_terminal_decision(candidate, subject_id, key):
    original = command(candidate)
    with pytest.raises(ExperienceError) as error:
        evaluate_review(
            candidate,
            command(candidate, idempotency_key=key, decision="reject"),
            subject_id=subject_id,
            existing=decision(candidate, original),
        )
    assert error.value.code == "experience_candidate_already_reviewed"


@pytest.mark.parametrize(
    "overrides",
    [
        {"actor_id": "forged"},
        {"decision": "approve"},
        {"reason": " "},
        {"reason": "长" * 1001},
        {"idempotency_key": "short"},
        {"idempotency_key": "not a valid key"},
        {"expected_candidate_content_hash": "A" * 64},
        {"expected_review_version": -1},
        {"expected_candidate_version": 0},
    ],
)
def test_review_command_rejects_unsafe_or_unbounded_inputs(candidate, overrides):
    with pytest.raises(ValidationError):
        command(candidate, **overrides)


def test_confirmation_wraps_original_artifact_without_rewriting_history(candidate):
    before = candidate.model_dump_json()
    content = ConfirmedExperienceContent(candidate_snapshot=candidate.content)
    assert content.candidate_snapshot == candidate.content
    assert "尚未经过经验审核" in content.candidate_snapshot.historical_summary
    assert "已由可信工程师确认为历史参考" in content.confirmation_notice
    assert "历史审批不授予当前执行权限" in content.confirmation_notice
    assert candidate.model_dump_json() == before
    with pytest.raises(ValidationError):
        content.schema_version = "forged"  # type: ignore[misc, assignment]
