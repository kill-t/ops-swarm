"""在既有预算事务内复核上下文材料与实际输入，禁止伪造审计清单。"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy, ModelInput
from opsswarm.investigation.budget.policy import operation_kind
from opsswarm.investigation.context.contracts import ContextManifest
from opsswarm.investigation.context.dispatch import build_context
from opsswarm.investigation.context.loading import load_sources
from opsswarm.investigation.loop.context import build_loop_context
from opsswarm.investigation.loop.contracts import AnyModelInput, LoopModelInput
from opsswarm.investigation.loop.ledger import saved_history
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict


async def validate_context_reservation(
    session: AsyncSession,
    run: InvestigationRunRow,
    operation: CallOperation,
    task_id: UUID | None,
    model_input: AnyModelInput | None,
    manifest: ContextManifest | None,
    prompt_version: str | None,
    task_attempt_id: UUID | None = None,
    invocation_index: int = 0,
) -> None:
    if isinstance(model_input, LoopModelInput) and (
        run.context_policy_version not in ("evidence-read-v1", "summary-context-v1")
        or task_id is None
    ):
        raise TaskFactConflict("context_policy_mismatch", "循环输入仅支持新策略的专业任务")
    if run.context_policy_version is None or operation_kind(operation) != "model":
        if manifest is not None:
            raise TaskFactConflict("context_not_allowed", "该调用不允许上下文清单")
        return
    if manifest is None or model_input is None or run.model_budget is None:
        raise TaskFactConflict("context_required", "启用上下文策略的模型调用必须提供材料清单")
    sources = await load_sources(session, run, operation, task_id)
    policy = ModelBudgetPolicy.model_validate(run.model_budget)
    if (
        run.context_policy_version in ("evidence-read-v1", "summary-context-v1")
        and task_id is not None
    ):
        if not isinstance(model_input, LoopModelInput) or task_attempt_id is None:
            raise TaskFactConflict("context_required", "新策略专业模型必须提供完整循环输入")
        history = await saved_history(session, run.id, task_attempt_id, invocation_index)
        expected = build_loop_context(sources, policy, history)
    else:
        if not isinstance(model_input, ModelInput):
            raise TaskFactConflict("context_policy_mismatch", "当前角色只接受原文本输入")
        expected = build_context(model_input, sources, operation, policy)
    if (
        expected.manifest != manifest
        or expected.model_input != model_input
        or prompt_version != manifest.prompt_version
    ):
        raise TaskFactConflict("context_manifest_mismatch", "上下文清单、实际输入与业务材料不一致")
