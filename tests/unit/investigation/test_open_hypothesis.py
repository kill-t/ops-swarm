"""开放假设保持自由文本、证据闭包与待审查边界。"""

import pytest
from pydantic import ValidationError

from opsswarm.investigation.synthesis.contracts import (
    OpenHypothesis,
    OpenHypothesisProposal,
)
from opsswarm.investigation.synthesis.policy import validate_proposal_evidence
from opsswarm.investigation.tasks.errors import TaskFactConflict


def proposal(**changes) -> OpenHypothesisProposal:
    values = {
        "statement": "数据库连接池耗尽可能解释当前异常",
        "confidence": 0.65,
        "supporting_evidence_ids": ("log-1",),
        "counter_evidence_ids": ("metric-1",),
        "unresolved_questions": ("变更窗口内是否还有配置漂移？",),
    }
    values.update(changes)
    return OpenHypothesisProposal.model_validate(values)


def test_system_injects_pending_review_without_legacy_cause_or_action_fields() -> None:
    hypothesis = OpenHypothesis.from_proposal(proposal())

    assert hypothesis.status == "pending_review"
    assert hypothesis.version == "open-hypothesis-v1"
    assert "root_cause" not in OpenHypothesis.model_fields
    assert "recommendation" not in OpenHypothesis.model_fields


def test_persisted_hypothesis_reuses_proposal_semantic_validation() -> None:
    with pytest.raises(ValidationError):
        OpenHypothesis.model_validate(
            {
                **OpenHypothesis.from_proposal(proposal()).model_dump(),
                "counter_evidence_ids": ("log-1",),
            }
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"supporting_evidence_ids": ("log-1", "log-1")},
        {"counter_evidence_ids": ("log-1",)},
        {"unresolved_questions": ()},
    ],
)
def test_proposal_rejects_ambiguous_evidence_or_missing_questions(changes) -> None:
    with pytest.raises(ValidationError):
        proposal(**changes)


def test_evidence_policy_rejects_reference_outside_supplied_materials() -> None:
    with pytest.raises(TaskFactConflict) as caught:
        validate_proposal_evidence(proposal(), {"log-1"})

    assert caught.value.code == "open_hypothesis_evidence_mismatch"
