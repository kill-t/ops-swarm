"""专业结果的边界与 v2 入口组合校验。"""

import pytest
from pydantic import ValidationError

from opsswarm.control_plane.schemas import StartInvestigationRequest
from opsswarm.investigation.models import SpecialistReport
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
from opsswarm.investigation.tasks.results import SpecialistTaskResult


def material():
    evidence = RELEASE_500_SCENARIO.evidence[0]
    return {
        "evidence_snapshot_id": "frozen:test",
        "evidence": [evidence.model_dump(mode="json")],
        "report": SpecialistReport(
            specialist=evidence.kind,
            summary="专业发现",
            findings=("需要进一步综合",),
            evidence_ids=(evidence.evidence_id,),
            confidence=0.6,
        ).model_dump(mode="json"),
    }


@pytest.mark.parametrize(
    "case",
    [
        "duplicate",
        "wrong_role",
        "unknown_reference",
        "missing_source",
        "unknown_source",
        "oversized",
    ],
)
def test_invalid_specialist_material_is_rejected(case):
    value = material()
    if case == "duplicate":
        value["evidence"] *= 2
    elif case == "wrong_role":
        value["report"]["specialist"] = "metric"
    elif case == "unknown_reference":
        value["report"]["evidence_ids"] = ["imagined-evidence"]
    elif case == "missing_source":
        value["evidence"][0]["source"] = ""
    elif case == "unknown_source":
        value["data_source_mode"] = "unknown"
    else:
        base = value["evidence"][0]
        value["evidence"] = [
            {**base, "evidence_id": f"log-{n}", "attributes": {"text": "a" * 14000}}
            for n in range(10)
        ]
        value["report"]["evidence_ids"] = ["log-0"]
    with pytest.raises(ValidationError):
        SpecialistTaskResult.model_validate(value)


@pytest.mark.parametrize("mode,source", [("single_agent", "frozen"), ("single_agent", "live")])
def test_v2_rejects_unsupported_routes(mode, source):
    with pytest.raises(ValidationError, match="v2 当前只支持"):
        StartInvestigationRequest(
            expected_version=1,
            actor_id="engineer",
            reason="测试入口",
            mode=mode,
            data_source_mode=source,
            workflow_version="v2",
        )


@pytest.mark.parametrize("source", ["frozen", "live"])
def test_default_v1_and_explicit_v2_are_compatible(source):
    fields = {"expected_version": 1, "actor_id": "engineer", "reason": "测试入口"}
    assert StartInvestigationRequest(**fields).workflow_version == "v1"
    assert (
        StartInvestigationRequest(
            **fields, mode="multi_agent", data_source_mode=source, workflow_version="v2"
        ).workflow_version
        == "v2"
    )
    assert SpecialistTaskResult.model_validate(material()).tool_call_count == 1
