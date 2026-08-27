import pytest
from pydantic import ValidationError

from opsswarm.evaluation import MVP_INVESTIGATION_DATASET_V1
from opsswarm.evaluation.models import InvestigationEvaluationDataset
from opsswarm.investigation.models import RootCause

EXPECTED_DATASET_HASH = "d3ded4e326b1aa0a8e06c38287ce22c6bc454574c6ef19b37e11cbc5803ecf7f"


def test_mvp_dataset_covers_three_versioned_faults_with_stable_hash() -> None:
    dataset = MVP_INVESTIGATION_DATASET_V1

    assert dataset.dataset_key == "opsswarm-mvp-investigation-v1"
    assert [item.scenario_id for item in dataset.scenarios] == [
        "release-500-baseline-v1",
        "database-pool-misconfiguration-v1",
        "redis-unavailable-v1",
    ]
    assert {item.expected_root_cause for item in dataset.scenarios} == {
        RootCause.RELEASE_500_REGRESSION,
        RootCause.DATABASE_POOL_MISCONFIGURATION,
        RootCause.REDIS_UNAVAILABLE,
    }
    assert dataset.content_hash() == EXPECTED_DATASET_HASH


def test_dataset_rejects_duplicate_or_silent_cross_version_scenarios() -> None:
    dataset = MVP_INVESTIGATION_DATASET_V1
    payload = dataset.model_dump(mode="json")

    duplicate_payload = {**payload, "scenarios": [payload["scenarios"][0]] * 2}
    with pytest.raises(ValidationError, match="scenario_id 不能重复"):
        InvestigationEvaluationDataset.model_validate(duplicate_payload)

    cross_version_payload = {**payload, "version": 2}
    with pytest.raises(ValidationError, match="场景版本必须与评测集版本一致"):
        InvestigationEvaluationDataset.model_validate(cross_version_payload)
