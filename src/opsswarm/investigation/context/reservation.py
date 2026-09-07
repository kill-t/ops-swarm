"""在既有预算事务内复核上下文材料与实际输入，禁止伪造审计清单。"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy, ModelInput
from opsswarm.investigation.budget.policy import operation_kind
from opsswarm.investigation.context.builder import ContextBuilder
from opsswarm.investigation.context.contracts import ContextManifest
from opsswarm.investigation.context.loading import load_sources
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict


async def validate_context_reservation(
    session: AsyncSession,
    run: InvestigationRunRow,
    operation: CallOperation,
    task_id: UUID | None,
    model_input: ModelInput | None,
    manifest: ContextManifest | None,
    prompt_version: str | None,
) -> None:
    if run.context_policy_version is None or operation_kind(operation) != "model":
        if manifest is not None:
            raise TaskFactConflict("context_not_allowed", "该调用不允许上下文清单")
        return
    if manifest is None or model_input is None or run.model_budget is None:
        raise TaskFactConflict("context_required", "启用上下文策略的模型调用必须提供材料清单")
    sources = await load_sources(session, run, operation, task_id)
    expected = ContextBuilder().build(
        model_input,
        sources,
        operation,
        ModelBudgetPolicy.model_validate(run.model_budget),
    )
    if (
        expected.manifest != manifest
        or expected.model_input != model_input
        or prompt_version != manifest.prompt_version
    ):
        raise TaskFactConflict("context_manifest_mismatch", "上下文清单、实际输入与业务材料不一致")
