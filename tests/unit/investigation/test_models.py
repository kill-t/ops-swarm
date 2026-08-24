from datetime import datetime

import pytest
from pydantic import ValidationError

from opsswarm.investigation.models import InvestigationConclusion


def test_rollback_recommendation_requires_human_approval() -> None:
    with pytest.raises(ValidationError, match="必须标记为需要人工审批"):
        InvestigationConclusion(
            root_cause="release_500_regression",
            confidence=0.9,
            summary="新版本可能导致回归。",
            evidence_ids=("change-order-release-rc1",),
            recommendation="rollback_release",
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
