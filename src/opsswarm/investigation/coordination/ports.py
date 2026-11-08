"""调查图依赖的首轮规划端口，不加载工作流实现。"""

from collections.abc import Awaitable
from datetime import datetime
from typing import Protocol

from opsswarm.investigation.coordination.contracts import (
    CoordinationPlanRecord,
    CoordinationPolicy,
)


class FirstRoundPlanner(Protocol):
    """动态图协调节点依赖的最小规划端口。"""

    def create_first_plan(
        self,
        *,
        policy: CoordinationPolicy,
        observation_cutoff_at: datetime,
        evidence_snapshot_id: str,
    ) -> Awaitable[tuple[CoordinationPlanRecord, bool, bool]]:
        """创建或复用首轮计划。"""
