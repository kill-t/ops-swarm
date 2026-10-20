"""基于真实业务闭环冻结来源的经验契约与纯投影测试。"""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.experience.contracts import (
    MAX_SOURCE_BYTES,
    ExperienceError,
    ExperienceSource,
    canonical_hash,
)
from opsswarm.experience.projection import project_source
from opsswarm.investigation.models import EvidenceObservation, EvidenceRelation
from opsswarm.reports.models import IncidentReportContent

SOURCE_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "experience-source-v1.json"


@pytest.fixture
def source() -> ExperienceSource:
    """仅消费真实 PostgreSQL 业务链导出的来源，不在单测伪造基线事实。"""
    return ExperienceSource.model_validate_json(SOURCE_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def source_data(source: ExperienceSource) -> dict[str, Any]:
    return deepcopy(source.model_dump(mode="json"))


def _rehash_report(data: dict[str, Any]) -> None:
    report_hash = canonical_hash(IncidentReportContent.model_validate(data["report"]["content"]))
    data["report"]["content_hash"] = report_hash
    data["report_content_hash"] = report_hash


def test_source_roundtrip_preserves_canonical_identity(source: ExperienceSource) -> None:
    restored = ExperienceSource.model_validate_json(source.model_dump_json())
    reversed_data = dict(reversed(list(source.model_dump(mode="json").items())))

    assert canonical_hash(restored) == canonical_hash(source)
    assert canonical_hash(ExperienceSource.model_validate(reversed_data)) == canonical_hash(source)


def test_json_nested_source_times_require_explicit_utc(source_data: dict[str, Any]) -> None:
    for path in (
        ("plan", "created_at"),
        ("hypothesis", "created_at"),
        ("hypothesis", "evidence_assessments", 0, "created_at"),
    ):
        data = deepcopy(source_data)
        target = data
        for segment in path[:-1]:
            target = target[segment]
        target[path[-1]] = target[path[-1]][:19]

        with pytest.raises(ValidationError, match="带时区的 UTC"):
            ExperienceSource.model_validate_json(json.dumps(data, ensure_ascii=False))


def test_evidence_content_tampering_is_rejected(source_data: dict[str, Any]) -> None:
    source_data["evidence"][0]["observation"]["summary"] = "被替换的观察摘要"

    with pytest.raises(ValidationError, match="证据内容与散列不一致"):
        ExperienceSource.model_validate(source_data)


def test_report_fingerprint_tampering_is_rejected(source_data: dict[str, Any]) -> None:
    source_data["report"]["content"]["incident"]["fingerprint"] = "changed-fingerprint"

    with pytest.raises(ValidationError, match="报告身份或调查摘要不一致"):
        ExperienceSource.model_validate(source_data)


@pytest.mark.parametrize("status", ["pending", "rejected", "expired"])
def test_historical_approval_must_have_been_approved(
    source_data: dict[str, Any],
    status: str,
) -> None:
    source_data["report"]["content"]["workroom"]["approval_request"]["status"] = status
    _rehash_report(source_data)
    with pytest.raises(ValidationError, match="计划与执行没有精确绑定"):
        ExperienceSource.model_validate(source_data)


def test_report_identity_cannot_be_replaced(source_data: dict[str, Any]) -> None:
    source_data["report_id"] = str(uuid4())

    with pytest.raises(ValidationError, match="报告身份或调查摘要不一致"):
        ExperienceSource.model_validate(source_data)


def test_incident_identity_cannot_cross_scope(source_data: dict[str, Any]) -> None:
    source_data["incident_id"] = str(uuid4())

    with pytest.raises(ValidationError, match="报告身份或调查摘要不一致"):
        ExperienceSource.model_validate(source_data)


def test_evidence_from_another_run_is_rejected(source_data: dict[str, Any]) -> None:
    source_data["evidence"][0]["investigation_run_id"] = str(uuid4())

    with pytest.raises(ValidationError, match="证据引用没有闭包"):
        ExperienceSource.model_validate(source_data)


def test_business_evidence_key_must_match_assessment(source_data: dict[str, Any]) -> None:
    source_data["hypothesis"]["evidence_assessments"][0]["evidence_id"] = "foreign-evidence-key"

    with pytest.raises(ValidationError, match="证据引用没有闭包"):
        ExperienceSource.model_validate(source_data)


def test_missing_assessed_evidence_is_rejected(source_data: dict[str, Any]) -> None:
    source_data["hypothesis"]["evidence_assessments"][0]["evidence_item_id"] = str(uuid4())

    with pytest.raises(ValidationError, match="证据引用没有闭包"):
        ExperienceSource.model_validate(source_data)


def test_duplicate_evidence_identity_is_rejected(source_data: dict[str, Any]) -> None:
    source_data["evidence"].append(deepcopy(source_data["evidence"][0]))

    with pytest.raises(ValidationError, match="证据引用没有闭包"):
        ExperienceSource.model_validate(source_data)


def test_approved_plan_parameters_cannot_drift(source_data: dict[str, Any]) -> None:
    parameters = source_data["plan"]["steps"][0]["parameters"]
    if "current_version" in parameters:
        parameters["current_version"] += "-changed"
    elif "current_value" in parameters:
        parameters["current_value"] = 999 if parameters["current_value"] != 999 else 998
    else:
        pytest.fail("真实来源夹具必须使用已有回滚业务链")

    with pytest.raises(ValidationError, match="历史审批的内容散列不一致"):
        ExperienceSource.model_validate(source_data)


def test_plan_from_another_run_is_rejected(source_data: dict[str, Any]) -> None:
    source_data["plan"]["investigation_run_id"] = str(uuid4())

    with pytest.raises(ValidationError, match="计划与执行没有精确绑定"):
        ExperienceSource.model_validate(source_data)


def test_execution_requires_exact_plan_version(source_data: dict[str, Any]) -> None:
    source_data["execution"]["action_plan_version"] += 1

    with pytest.raises(ValidationError, match="计划与执行没有精确绑定"):
        ExperienceSource.model_validate(source_data)


def test_verification_requires_exact_execution(source_data: dict[str, Any]) -> None:
    source_data["verification"]["operation_execution_id"] = str(uuid4())

    with pytest.raises(ValidationError, match="验证没有精确绑定"):
        ExperienceSource.model_validate(source_data)


def test_samples_cannot_cross_verification_run(source_data: dict[str, Any]) -> None:
    source_data["verification"]["samples"][0]["verification_run_id"] = str(uuid4())

    with pytest.raises(ValidationError, match="样本必须属于当前运行"):
        ExperienceSource.model_validate(source_data)


def test_sample_threshold_cannot_drift_from_frozen_rule(source_data: dict[str, Any]) -> None:
    sample = source_data["verification"]["samples"][0]
    sample["threshold"] = 1.0 if sample["threshold"] != 1.0 else 0.9

    with pytest.raises(ValidationError, match="样本必须使用运行冻结的规则"):
        ExperienceSource.model_validate(source_data)


def test_investigation_mode_cannot_be_relabelled(source_data: dict[str, Any]) -> None:
    source_data["data_source_mode"] = (
        "live" if source_data["data_source_mode"] == "frozen" else "frozen"
    )

    with pytest.raises(ValidationError, match="报告身份或调查摘要不一致"):
        ExperienceSource.model_validate(source_data)


def test_source_size_limit_does_not_silently_clip_material(source_data: dict[str, Any]) -> None:
    source_data["report"]["content"]["timeline"][0]["payload"]["bounded_test_material"] = (
        "x" * MAX_SOURCE_BYTES
    )
    _rehash_report(source_data)

    with pytest.raises(ExperienceError) as error:
        ExperienceSource.model_validate(source_data)

    assert error.value.code == "source_too_large"
    assert "bounded_test_material" not in error.value.message


def test_evidence_limit_is_enforced_before_projection(source_data: dict[str, Any]) -> None:
    while len(source_data["evidence"]) <= 20:
        item = deepcopy(source_data["evidence"][0])
        item["id"] = str(uuid4())
        item["observation"]["evidence_id"] = f"extra-evidence-{len(source_data['evidence'])}"
        item["content_hash"] = canonical_hash(
            EvidenceObservation.model_validate(item["observation"])
        )
        source_data["evidence"].append(item)

    with pytest.raises(ValidationError, match="at most 20"):
        ExperienceSource.model_validate(source_data)


def test_projection_is_deterministic_and_keeps_original_judgment(source: ExperienceSource) -> None:
    first = project_source(source)
    second = project_source(ExperienceSource.model_validate_json(source.model_dump_json()))

    assert canonical_hash(first) == canonical_hash(second)
    assert first.hypothesis_status == source.hypothesis.status.value
    assert source.hypothesis.statement in first.historical_summary
    assert first.sample_ids == tuple(item.id for item in source.verification.samples)


def test_projection_keeps_support_counterevidence_and_context(source_data: dict[str, Any]) -> None:
    assessment = source_data["hypothesis"]["evidence_assessments"][0]
    for relation in EvidenceRelation:
        data = deepcopy(source_data)
        data["hypothesis"]["evidence_assessments"][0]["relation"] = relation.value
        source = ExperienceSource.model_validate(data)

        draft = project_source(source)
        reference = next(
            item
            for item in draft.evidence_refs
            if str(item.evidence_item_id) == assessment["evidence_item_id"]
        )

        assert reference.relations == (relation,)
        assert reference.evidence_id == assessment["evidence_id"]


def test_draft_excludes_execution_parameters_and_historical_approval(
    source: ExperienceSource,
) -> None:
    draft = project_source(source)
    draft_data = draft.model_dump(mode="json")
    serialized = json.dumps(draft_data, ensure_ascii=False)

    assert not {
        "plan",
        "execution",
        "action_type",
        "parameters",
        "approval_request_id",
        "approval_plan_content_hash",
    }.intersection(draft_data)
    assert str(source.execution.approval_request_id) not in serialized
    assert "尚未经过经验审核" in draft.historical_summary
    assert draft.recheck_requirement == "使用前必须重新调查当前事故并核对适用范围及反证"
    assert draft.authorization_limit == "历史审批及操作不授予当前事故的审批或执行权限"
    assert draft.verifier_name == source.verification.verifier_name
    assert draft.data_source_mode == source.data_source_mode


@pytest.mark.parametrize("target", ["source", "draft"])
def test_contracts_are_frozen_and_reject_extra_fields(
    source: ExperienceSource, target: str
) -> None:
    model = source if target == "source" else project_source(source)
    data = deepcopy(model.model_dump(mode="json"))
    data["execution_authorized"] = True

    with pytest.raises(ValidationError, match="Extra inputs"):
        type(model).model_validate(data)
    with pytest.raises(ValidationError, match="frozen"):
        model.environment = "production"  # type: ignore[misc, assignment]
