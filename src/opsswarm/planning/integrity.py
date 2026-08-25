"""审批绑定计划内容所需的稳定摘要。"""

import hashlib
import json
from uuid import UUID

from opsswarm.planning.models import ActionPlan


def action_plan_content_hash(
    plan: ActionPlan,
    *,
    plan_id: UUID,
    investigation_run_id: UUID,
    hypothesis_id: UUID,
) -> str:
    """计算安全相关内容摘要；生命周期状态不属于获批动作范围。"""

    canonical = json.dumps(
        {
            "plan_id": str(plan_id),
            "investigation_run_id": str(investigation_run_id),
            "hypothesis_id": str(hypothesis_id),
            "version": plan.version,
            "summary": plan.summary,
            "risk_level": plan.risk_level.value,
            "requires_human_approval": plan.requires_human_approval,
            "risk_policy_version": plan.risk_policy_version,
            "steps": [step.model_dump(mode="json") for step in plan.steps],
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
