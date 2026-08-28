from datetime import datetime

import pytest
from pydantic import ValidationError

from opsswarm.investigation.models import (
    EvidenceObservation,
    InvestigationArtifacts,
    InvestigationConclusion,
    InvestigationRunResult,
    SpecialistReport,
)
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO


@pytest.mark.parametrize(
    "recommendation",
    [
        "rollback_release",
        "rollback_configuration",
        "restore_dependency",
    ],
)
def test_mitigation_recommendation_requires_human_approval(recommendation: str) -> None:
    with pytest.raises(ValidationError, match="必须标记为需要人工审批"):
        InvestigationConclusion(
            root_cause="release_500_regression",
            confidence=0.9,
            summary="新版本可能导致回归。",
            evidence_ids=("change-order-release-rc1",),
            recommendation=recommendation,
            requires_human_approval=False,
        )


def test_investigation_request_rejects_naive_time() -> None:
    from uuid import uuid4

    from opsswarm.investigation.models import InvestigationRequest

    with pytest.raises(ValidationError, match="必须包含时区"):
        InvestigationRequest(
            incident_id=uuid4(),
            service_name="order-service",
            title="订单接口异常",
            started_at=datetime(2026, 1, 1, 8, 0),
        )


def make_result() -> InvestigationRunResult:
    evidence_ids = tuple(sorted(RELEASE_500_SCENARIO.required_evidence_ids))
    return InvestigationRunResult(
        conclusion=InvestigationConclusion(
            root_cause="release_500_regression",
            confidence=0.95,
            summary="三类证据共同指向版本回归。",
            evidence_ids=evidence_ids,
            recommendation="rollback_release",
            requires_human_approval=True,
        ),
        returned_evidence_ids=evidence_ids,
        tool_call_count=3,
    )


def test_evidence_board_must_contain_every_returned_evidence() -> None:
    with pytest.raises(ValidationError, match="必须与 Evidence Board 证据完全一致"):
        InvestigationArtifacts(
            result=make_result(),
            evidence_items=RELEASE_500_SCENARIO.evidence[:2],
        )


def test_returned_evidence_ids_cannot_repeat() -> None:
    with pytest.raises(ValidationError, match="returned_evidence_ids 不能重复"):
        InvestigationRunResult(
            conclusion=make_result().conclusion,
            returned_evidence_ids=(
                "log-order-runtime-error",
                "log-order-runtime-error",
            ),
            tool_call_count=2,
        )


def test_evidence_attributes_cannot_contain_large_raw_payload() -> None:
    with pytest.raises(ValidationError, match="不能超过 16 KiB"):
        EvidenceObservation(
            evidence_id="log-oversized-payload",
            kind="log",
            source="test-log-store",
            summary="属性中错误地包含了大段原始日志。",
            observed_at="2026-01-01T08:00:00Z",
            attributes={"raw_log": "故" * (16 * 1024)},
        )


def test_specialist_report_cannot_reference_another_specialty() -> None:
    with pytest.raises(ValidationError, match="只能引用同类专业证据"):
        InvestigationArtifacts(
            result=make_result(),
            evidence_items=RELEASE_500_SCENARIO.evidence,
            specialist_reports=(
                SpecialistReport(
                    specialist="log",
                    summary="错误地引用了变更证据。",
                    findings=("发现发布记录。",),
                    evidence_ids=("change-order-release-rc1",),
                    confidence=0.5,
                ),
            ),
        )
