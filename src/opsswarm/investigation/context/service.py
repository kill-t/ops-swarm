"""组织上下文读取和纯组装，事务提交仍由预算仓储负责。"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy, ModelInput
from opsswarm.investigation.context.builder import ContextBuilder
from opsswarm.investigation.context.contracts import ContextPacket
from opsswarm.investigation.context.loading import load_sources
from opsswarm.investigation.errors import InvestigationRunNotFound
from opsswarm.investigation.persistence.tables import InvestigationRunRow


class TaskContextService:
    def __init__(self, factory: async_sessionmaker[AsyncSession], run_id: UUID) -> None:
        self._factory = factory
        self._run_id = run_id

    async def prepare(
        self,
        supplied: ModelInput,
        operation: CallOperation,
        task_id: UUID | None,
        policy: ModelBudgetPolicy,
    ) -> ContextPacket:
        async with self._factory() as session:
            run = await session.get(InvestigationRunRow, self._run_id)
            if run is None:
                raise InvestigationRunNotFound(self._run_id)
            sources = await load_sources(session, run, operation, task_id)
        return ContextBuilder().build(supplied, sources, operation, policy)
