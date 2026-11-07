"""OpsSwarm 调查领域、单 Agent 与 LangGraph 并行基线。"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opsswarm.investigation.agent import SingleAgentInvestigator
    from opsswarm.investigation.challenge import challenge_investigation
    from opsswarm.investigation.changes import (
        ChangeApiOnlyInvestigationDataSource,
        ChangeInvestigationSettings,
    )
    from opsswarm.investigation.evaluation import (
        compare_investigation_modes,
        evaluate_investigation,
    )
    from opsswarm.investigation.github_changes import (
        GitHubChangeOnlyInvestigationDataSource,
        GitHubChangeSettings,
        GitHubEnrichedChangeDataSource,
    )
    from opsswarm.investigation.live import (
        LiveChangeProvider,
        LiveInvestigationDataSource,
        LiveInvestigationSettings,
        create_live_investigation_data_source,
    )
    from opsswarm.investigation.loki import (
        LokiInvestigationSettings,
        LokiOnlyInvestigationDataSource,
    )
    from opsswarm.investigation.models import (
        EvidenceKind,
        EvidenceObservation,
        EvidenceRelation,
        HypothesisChallenge,
        HypothesisEvidenceAssessment,
        HypothesisStatus,
        InvestigationArtifacts,
        InvestigationComparison,
        InvestigationConclusion,
        InvestigationDataSourceMode,
        InvestigationMetrics,
        InvestigationMode,
        InvestigationRequest,
        InvestigationRunResult,
        InvestigationRunStatus,
        MultiAgentInvestigationResult,
        Recommendation,
        RootCause,
        SpecialistReport,
        WorkItemKind,
        WorkItemStatus,
    )
    from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator
    from opsswarm.investigation.prometheus import (
        PrometheusInvestigationSettings,
        PrometheusOnlyInvestigationDataSource,
    )

__all__ = [
    "ChangeApiOnlyInvestigationDataSource",
    "ChangeInvestigationSettings",
    "EvidenceKind",
    "EvidenceObservation",
    "EvidenceRelation",
    "GitHubChangeOnlyInvestigationDataSource",
    "GitHubChangeSettings",
    "GitHubEnrichedChangeDataSource",
    "HypothesisChallenge",
    "HypothesisEvidenceAssessment",
    "HypothesisStatus",
    "InvestigationArtifacts",
    "InvestigationComparison",
    "InvestigationConclusion",
    "InvestigationDataSourceMode",
    "InvestigationMetrics",
    "InvestigationMode",
    "InvestigationRequest",
    "InvestigationRunStatus",
    "InvestigationRunResult",
    "LangGraphParallelInvestigator",
    "LiveChangeProvider",
    "LiveInvestigationDataSource",
    "LiveInvestigationSettings",
    "LokiInvestigationSettings",
    "LokiOnlyInvestigationDataSource",
    "MultiAgentInvestigationResult",
    "PrometheusInvestigationSettings",
    "PrometheusOnlyInvestigationDataSource",
    "Recommendation",
    "RootCause",
    "SingleAgentInvestigator",
    "SpecialistReport",
    "WorkItemKind",
    "WorkItemStatus",
    "compare_investigation_modes",
    "challenge_investigation",
    "create_live_investigation_data_source",
    "evaluate_investigation",
]

# 读取数据契约无需加载模型运行器或演练 HTTP 应用；保留既有包级导出。
_EXPORT_MODULES = {
    "SingleAgentInvestigator": "opsswarm.investigation.agent",
    "challenge_investigation": "opsswarm.investigation.challenge",
    "ChangeApiOnlyInvestigationDataSource": "opsswarm.investigation.changes",
    "ChangeInvestigationSettings": "opsswarm.investigation.changes",
    "compare_investigation_modes": "opsswarm.investigation.evaluation",
    "evaluate_investigation": "opsswarm.investigation.evaluation",
    "GitHubChangeOnlyInvestigationDataSource": "opsswarm.investigation.github_changes",
    "GitHubChangeSettings": "opsswarm.investigation.github_changes",
    "GitHubEnrichedChangeDataSource": "opsswarm.investigation.github_changes",
    "LiveChangeProvider": "opsswarm.investigation.live",
    "LiveInvestigationDataSource": "opsswarm.investigation.live",
    "LiveInvestigationSettings": "opsswarm.investigation.live",
    "create_live_investigation_data_source": "opsswarm.investigation.live",
    "LokiInvestigationSettings": "opsswarm.investigation.loki",
    "LokiOnlyInvestigationDataSource": "opsswarm.investigation.loki",
    "EvidenceKind": "opsswarm.investigation.models",
    "EvidenceObservation": "opsswarm.investigation.models",
    "EvidenceRelation": "opsswarm.investigation.models",
    "HypothesisChallenge": "opsswarm.investigation.models",
    "HypothesisEvidenceAssessment": "opsswarm.investigation.models",
    "HypothesisStatus": "opsswarm.investigation.models",
    "InvestigationArtifacts": "opsswarm.investigation.models",
    "InvestigationComparison": "opsswarm.investigation.models",
    "InvestigationConclusion": "opsswarm.investigation.models",
    "InvestigationDataSourceMode": "opsswarm.investigation.models",
    "InvestigationMetrics": "opsswarm.investigation.models",
    "InvestigationMode": "opsswarm.investigation.models",
    "InvestigationRequest": "opsswarm.investigation.models",
    "InvestigationRunResult": "opsswarm.investigation.models",
    "InvestigationRunStatus": "opsswarm.investigation.models",
    "MultiAgentInvestigationResult": "opsswarm.investigation.models",
    "Recommendation": "opsswarm.investigation.models",
    "RootCause": "opsswarm.investigation.models",
    "SpecialistReport": "opsswarm.investigation.models",
    "WorkItemKind": "opsswarm.investigation.models",
    "WorkItemStatus": "opsswarm.investigation.models",
    "LangGraphParallelInvestigator": "opsswarm.investigation.parallel_graph",
    "PrometheusInvestigationSettings": "opsswarm.investigation.prometheus",
    "PrometheusOnlyInvestigationDataSource": "opsswarm.investigation.prometheus",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"模块 {__name__!r} 没有属性 {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
