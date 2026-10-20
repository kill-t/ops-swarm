"""把任务范围固定为目录后提供受限调查手册读取。"""

from opsswarm.investigation.models import InvestigationRequest
from opsswarm.investigation.runbooks.contracts import (
    ReadRunbookArguments,
    RunbookCatalog,
    RunbookDocument,
)
from opsswarm.investigation.runbooks.registry import RunbookRegistry
from opsswarm.investigation.tasks.evidence import TaskEvidenceRecord


def select_runbooks(
    request: InvestigationRequest,
    task_evidence_record: TaskEvidenceRecord,
    *,
    registry: RunbookRegistry | None = None,
) -> RunbookCatalog:
    """从已校验任务快照选择目录，不读取标题或预期根因。"""

    return (registry or RunbookRegistry()).catalog(request, task_evidence_record.snapshot)


def load_runbook(
    arguments: ReadRunbookArguments,
    *,
    catalog: RunbookCatalog,
    registry: RunbookRegistry | None = None,
) -> RunbookDocument:
    """只加载已选目录中的精确版本，目录由调用方从系统范围注入。"""

    return (registry or RunbookRegistry()).read(catalog, arguments)


class RunbookReader:
    """构造时注入任务事实，模型参数不能改变服务、角色或快照。"""

    def __init__(
        self,
        *,
        request: InvestigationRequest,
        task_evidence_record: TaskEvidenceRecord,
        registry: RunbookRegistry | None = None,
    ) -> None:
        self._registry = registry or RunbookRegistry()
        self._catalog = select_runbooks(
            request,
            task_evidence_record,
            registry=self._registry,
        )

    @property
    def catalog(self) -> RunbookCatalog:
        return self._catalog

    def read(self, arguments: ReadRunbookArguments) -> RunbookDocument:
        return load_runbook(arguments, catalog=self._catalog, registry=self._registry)

    def load_runbook(self, arguments: ReadRunbookArguments) -> RunbookDocument:
        """提供与工具名一致的显式入口。"""

        return self.read(arguments)
