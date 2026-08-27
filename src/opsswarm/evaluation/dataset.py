"""OpsSwarm MVP 调查评测集的稳定入口。"""

from opsswarm.evaluation.models import InvestigationEvaluationDataset
from opsswarm.investigation.scenarios import (
    DATABASE_POOL_SCENARIO,
    REDIS_UNAVAILABLE_SCENARIO,
    RELEASE_500_SCENARIO,
)

SCORING_POLICY_VERSION = "investigation-scoring-v1"

MVP_INVESTIGATION_DATASET_V1 = InvestigationEvaluationDataset(
    dataset_id="opsswarm-mvp-investigation",
    version=1,
    scoring_policy_version=SCORING_POLICY_VERSION,
    scenarios=(
        RELEASE_500_SCENARIO,
        DATABASE_POOL_SCENARIO,
        REDIS_UNAVAILABLE_SCENARIO,
    ),
)
