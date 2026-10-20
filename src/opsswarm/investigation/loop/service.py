"""组织持久目录、交互历史与原证据读取用例。"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.contracts import ContextPacket
from opsswarm.investigation.context.evidence_contracts import EvidenceReadRequest
from opsswarm.investigation.context.evidence_reader import ContextEvidenceReader
from opsswarm.investigation.context.loading import load_sources
from opsswarm.investigation.loop.context import build_loop_context
from opsswarm.investigation.loop.contracts import (
    AssistantMessage,
    LoopModelResult,
    ReadEvidenceArguments,
)
from opsswarm.investigation.loop.history import saved_history, saved_runbook_catalog
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.runbooks.contracts import ReadRunbookArguments, RunbookReadDetail
from opsswarm.investigation.runbooks.reader import RunbookReader
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.tables import AgentTaskRow


class EvidenceLoopService:
    def __init__(self, factory: async_sessionmaker[AsyncSession], run_id: UUID) -> None:
        self._factory, self._run_id = factory, run_id

    async def prepare(
        self,
        task_id: UUID,
        attempt_id: UUID,
        operation: CallOperation,
        invocation_index: int,
        policy: ModelBudgetPolicy,
    ) -> ContextPacket:
        async with self._factory() as session:
            run = await session.get(InvestigationRunRow, self._run_id)
            if run is None:
                raise TaskFactConflict("context_scope_mismatch", "运行不存在")
            sources = await load_sources(session, run, operation, task_id)
            history = await saved_history(session, run.id, attempt_id, invocation_index)
            fixed_catalog = (
                await saved_runbook_catalog(session, run.id, task_id)
                if run.context_policy_version == "runbook-read-v1"
                else None
            )
            return build_loop_context(
                sources,
                policy,
                history,
                fixed_runbook_catalog=fixed_catalog,
            )

    async def read(self, call_id: UUID, task_id: UUID, arguments: ReadEvidenceArguments) -> str:
        reader = ContextEvidenceReader(
            run_id=self._run_id,
            call_id=call_id,
            calls=CallBudgetQueries(self._factory),
            tasks=CollaborationQueries(self._factory),
        )
        detail = await reader.read(
            EvidenceReadRequest(
                task_id=task_id,
                evidence_id=arguments.evidence_id,
                snapshot_sha256=arguments.snapshot_sha256,
            )
        )
        return detail.model_dump_json()

    async def read_runbook(
        self,
        call_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        arguments: ReadRunbookArguments,
    ) -> str:
        async with self._factory() as session:
            run = await session.get(InvestigationRunRow, self._run_id)
            task = await session.get(AgentTaskRow, task_id)
            source = await session.get(CallReservationRow, call_id)
            if (
                run is None
                or run.context_policy_version != "runbook-read-v1"
                or task is None
                or task.investigation_run_id != run.id
                or task.status != "running"
                or source is None
                or source.investigation_run_id != run.id
                or source.task_id != task_id
                or source.task_attempt_id != attempt_id
                or source.kind != "model"
                or source.status != "succeeded"
                or source.call_result is None
            ):
                raise TaskFactConflict("runbook_scope_mismatch", "手册读取不属于当前有效专业尝试")
            result = LoopModelResult.model_validate(source.call_result)
            message = result.messages[0]
            if (
                len(result.messages) != 1
                or not isinstance(message, AssistantMessage)
                or message.tool_calls[0].name != "read_runbook"
                or message.tool_calls[0].args != arguments.model_dump(mode="json")
            ):
                raise TaskFactConflict("runbook_scope_mismatch", "手册参数与持久模型请求不一致")
            operation = CallOperation(f"analyze_{task.assignment['role']}")
            sources = await load_sources(session, run, operation, task_id)
            record = sources.tasks[0].evidence
            document = RunbookReader(
                request=sources.request,
                task_evidence_record=record,
            ).read(arguments)
            return RunbookReadDetail(
                investigation_run_id=run.id,
                call_id=call_id,
                task_id=task_id,
                document=document,
            ).model_dump_json()
