"""OpsSwarm MVP 调查评测集的稳定入口。"""

from opsswarm.evaluation.models import InvestigationEvaluationDataset
from opsswarm.investigation.scenarios import (
    DATABASE_POOL_SCENARIO,
    DATABASE_POOL_SCENARIO_V2,
    REDIS_UNAVAILABLE_SCENARIO,
    REDIS_UNAVAILABLE_SCENARIO_V2,
    RELEASE_500_SCENARIO,
    RELEASE_500_SCENARIO_V2,
)

SCORING_POLICY_VERSION_V1 = "investigation-scoring-v1"
SCORING_POLICY_VERSION_V2 = "investigation-scoring-v2"
SCORING_POLICY_VERSION = SCORING_POLICY_VERSION_V1

MVP_INVESTIGATION_DATASET_V1 = InvestigationEvaluationDataset(
    dataset_id="opsswarm-mvp-investigation",
    version=1,
    scoring_policy_version=SCORING_POLICY_VERSION_V1,
    scenarios=(
        RELEASE_500_SCENARIO,
        DATABASE_POOL_SCENARIO,
        REDIS_UNAVAILABLE_SCENARIO,
    ),
)

MVP_INVESTIGATION_DATASET_V2 = InvestigationEvaluationDataset(
    dataset_id="opsswarm-mvp-investigation",
    version=2,
    scoring_policy_version=SCORING_POLICY_VERSION_V2,
    scenarios=(
        RELEASE_500_SCENARIO_V2,
        DATABASE_POOL_SCENARIO_V2,
        REDIS_UNAVAILABLE_SCENARIO_V2,
    ),
)

LATEST_MVP_INVESTIGATION_DATASET = MVP_INVESTIGATION_DATASET_V2


def get_mvp_investigation_dataset(version: int) -> InvestigationEvaluationDataset:
    """按显式版本返回冻结数据集，不允许静默回退到最新版。"""

    datasets = {
        1: MVP_INVESTIGATION_DATASET_V1,
        2: MVP_INVESTIGATION_DATASET_V2,
    }
    try:
        return datasets[version]
    except KeyError as error:
        raise ValueError(f"不支持的评测集版本：{version}") from error
