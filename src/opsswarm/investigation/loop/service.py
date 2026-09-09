"""组织持久目录、交互历史与原证据读取用例。"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.context.contracts import ContextPacket
from opsswarm.investigation.context.evidence_contracts import EvidenceReadRequest
from opsswarm.investigation.context.evidence_reader import ContextEvidenceReader
from opsswarm.investigation.context.loading import load_sources
from opsswarm.investigation.loop.context import build_loop_context
from opsswarm.investigation.loop.contracts import ReadEvidenceArguments
from opsswarm.investigation.loop.ledger import saved_history
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries


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
            return build_loop_context(sources, policy, history)

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
