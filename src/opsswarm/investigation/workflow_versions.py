"""调查运行的版本选择与兼容范围。"""

from enum import StrEnum

from opsswarm.investigation.models import InvestigationDataSourceMode, InvestigationMode


class WorkflowVersion(StrEnum):
    V1 = "v1"
    V2 = "v2"


def validate_workflow_route(
    version: WorkflowVersion,
    mode: InvestigationMode,
    data_source_mode: InvestigationDataSourceMode,
) -> None:
    if version is WorkflowVersion.V2 and mode is not InvestigationMode.MULTI_AGENT:
        raise ValueError("v2 当前只支持多 Agent 调查")
