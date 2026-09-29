"""调查运行的版本选择与兼容范围。"""

from enum import StrEnum
from typing import Literal

from opsswarm.investigation.models import InvestigationDataSourceMode, InvestigationMode


class WorkflowVersion(StrEnum):
    V1 = "v1"
    V2 = "v2"
    V3 = "v3"


InvestigationGraphVersion = Literal[
    "dynamic-investigation-graph-v1",
    "dynamic-investigation-graph-v2",
    "dynamic-investigation-graph-v3",
    "dynamic-investigation-graph-v4",
    "dynamic-investigation-graph-v5",
    "dynamic-investigation-graph-v6",
    "dynamic-investigation-graph-v7",
    "dynamic-investigation-graph-v8",
]
LEGACY_DYNAMIC_COORDINATION_GRAPH_VERSION: InvestigationGraphVersion = (
    "dynamic-investigation-graph-v1"
)
LEGACY_DYNAMIC_FIRST_ROUND_GRAPH_VERSION: InvestigationGraphVersion = (
    "dynamic-investigation-graph-v2"
)
LEGACY_DYNAMIC_SYNTHESIS_GRAPH_VERSION: InvestigationGraphVersion = "dynamic-investigation-graph-v3"
LEGACY_DYNAMIC_REVIEW_GRAPH_VERSION: InvestigationGraphVersion = "dynamic-investigation-graph-v4"
LEGACY_DYNAMIC_ROUTING_GRAPH_VERSION: InvestigationGraphVersion = "dynamic-investigation-graph-v5"
LEGACY_DYNAMIC_FOLLOW_UP_GRAPH_VERSION: InvestigationGraphVersion = "dynamic-investigation-graph-v6"
LEGACY_DYNAMIC_SECOND_ROUND_GRAPH_VERSION: InvestigationGraphVersion = (
    "dynamic-investigation-graph-v7"
)
DYNAMIC_INVESTIGATION_GRAPH_VERSION: InvestigationGraphVersion = "dynamic-investigation-graph-v8"
DYNAMIC_SYNTHESIS_GRAPH_VERSIONS = frozenset(
    (
        LEGACY_DYNAMIC_SYNTHESIS_GRAPH_VERSION,
        LEGACY_DYNAMIC_REVIEW_GRAPH_VERSION,
        LEGACY_DYNAMIC_ROUTING_GRAPH_VERSION,
        LEGACY_DYNAMIC_FOLLOW_UP_GRAPH_VERSION,
        LEGACY_DYNAMIC_SECOND_ROUND_GRAPH_VERSION,
        DYNAMIC_INVESTIGATION_GRAPH_VERSION,
    )
)
DYNAMIC_REVIEW_GRAPH_VERSIONS = frozenset(
    (
        LEGACY_DYNAMIC_REVIEW_GRAPH_VERSION,
        LEGACY_DYNAMIC_ROUTING_GRAPH_VERSION,
        LEGACY_DYNAMIC_FOLLOW_UP_GRAPH_VERSION,
        LEGACY_DYNAMIC_SECOND_ROUND_GRAPH_VERSION,
        DYNAMIC_INVESTIGATION_GRAPH_VERSION,
    )
)
DYNAMIC_ROUTING_GRAPH_VERSIONS = frozenset(
    (
        LEGACY_DYNAMIC_ROUTING_GRAPH_VERSION,
        LEGACY_DYNAMIC_FOLLOW_UP_GRAPH_VERSION,
        LEGACY_DYNAMIC_SECOND_ROUND_GRAPH_VERSION,
        DYNAMIC_INVESTIGATION_GRAPH_VERSION,
    )
)
DYNAMIC_FOLLOW_UP_GRAPH_VERSIONS = frozenset(
    (
        LEGACY_DYNAMIC_FOLLOW_UP_GRAPH_VERSION,
        LEGACY_DYNAMIC_SECOND_ROUND_GRAPH_VERSION,
        DYNAMIC_INVESTIGATION_GRAPH_VERSION,
    )
)
DYNAMIC_SECOND_ROUND_GRAPH_VERSIONS = frozenset(
    (LEGACY_DYNAMIC_SECOND_ROUND_GRAPH_VERSION, DYNAMIC_INVESTIGATION_GRAPH_VERSION)
)
DYNAMIC_SECOND_ROUND_SYNTHESIS_GRAPH_VERSIONS = frozenset((DYNAMIC_INVESTIGATION_GRAPH_VERSION,))
DYNAMIC_INVESTIGATION_GRAPH_VERSIONS = frozenset(
    (
        LEGACY_DYNAMIC_COORDINATION_GRAPH_VERSION,
        LEGACY_DYNAMIC_FIRST_ROUND_GRAPH_VERSION,
        LEGACY_DYNAMIC_SYNTHESIS_GRAPH_VERSION,
        LEGACY_DYNAMIC_REVIEW_GRAPH_VERSION,
        LEGACY_DYNAMIC_ROUTING_GRAPH_VERSION,
        LEGACY_DYNAMIC_FOLLOW_UP_GRAPH_VERSION,
        LEGACY_DYNAMIC_SECOND_ROUND_GRAPH_VERSION,
        DYNAMIC_INVESTIGATION_GRAPH_VERSION,
    )
)


def validate_workflow_route(
    version: WorkflowVersion,
    mode: InvestigationMode,
    data_source_mode: InvestigationDataSourceMode,
) -> None:
    if version is WorkflowVersion.V2 and mode is not InvestigationMode.MULTI_AGENT:
        raise ValueError("v2 当前只支持多 Agent 调查")
    if version is WorkflowVersion.V3:
        if mode is not InvestigationMode.MULTI_AGENT:
            raise ValueError("v3 当前只支持多 Agent 调查")
        if data_source_mode is not InvestigationDataSourceMode.FROZEN:
            raise ValueError("v3 启动契约当前只支持冻结数据源")
