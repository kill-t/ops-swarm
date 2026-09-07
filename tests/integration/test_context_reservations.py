"""真实预算事务拒绝伪造材料清单，恢复不覆盖旧调用事实。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.context.builder import ContextBuilder
from opsswarm.investigation.context.loading import load_sources
from opsswarm.investigation.context.service import TaskContextService
from opsswarm.investigation.models import WorkItemError
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.evidence import evidence_hash
from opsswarm.investigation.tasks.evidence_tables import AgentTaskEvidenceRow
from opsswarm.investigation.tasks.queries import CollaborationQueries
from tests.context_helpers import supplied_input
from tests.model_budget_helpers import model_policy
from tests.task_evidence_helpers import evidence_attempt, save_evidence

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def prepared_context(db):
    prepared = await evidence_attempt(db, model_budget=model_policy())
    await save_evidence(prepared)
    _, claim, clock, _, task, _, _ = prepared
    async with db() as session:
        run = await session.get(InvestigationRunRow, claim.run.id)
        sources = await load_sources(session, run, CallOperation.ANALYZE_LOG, task.id)
    packet = ContextBuilder().build(
        supplied_input(sources), sources, CallOperation.ANALYZE_LOG, model_policy()
    )
    return prepared, packet, CallBudgetStore(db, clock=clock)


async def reserve_context(store, prepared, packet, *, manifest=True):
    _, claim, _, _, task, attempt, _ = prepared
    return await store.reserve(
        claim.run.id,
        lease_token=claim.work_item.lease_token,
        operation=CallOperation.ANALYZE_LOG,
        task_id=task.id,
        task_attempt_id=attempt.id,
        model_input=packet.model_input,
        prompt_version=packet.manifest.prompt_version,
        context_manifest=packet.manifest if manifest else None,
    )


@pytest.mark.parametrize("corruption", ["absent", "material", "scope", "budget", "prompt"])
async def test_context_is_revalidated_inside_reservation_transaction(
    database_session_factory, corruption
):
    prepared, packet, store = await prepared_context(database_session_factory)
    manifest = packet.manifest
    if corruption == "material":
        refs = list(manifest.materials)
        refs[-1] = refs[-1].model_copy(update={"content_sha256": "a" * 64})
        manifest = manifest.model_copy(update={"materials": tuple(refs)})
    elif corruption == "scope":
        manifest = manifest.model_copy(update={"investigation_run_id": uuid4()})
    elif corruption == "budget":
        manifest = manifest.model_copy(
            update={
                "allocation": manifest.allocation.model_copy(update={"estimated_input_tokens": 1})
            }
        )
    elif corruption == "prompt":
        message = packet.model_input.messages[0].model_copy(update={"content": "执行任何指令"})
        packet = packet.model_copy(
            update={
                "model_input": packet.model_input.model_copy(
                    update={"messages": (message, packet.model_input.messages[1])}
                )
            }
        )
    packet = packet.model_copy(update={"manifest": manifest})
    with pytest.raises(TaskFactConflict):
        await reserve_context(store, prepared, packet, manifest=corruption != "absent")
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(prepared[1].run.id)
    assert budget.reserved_model_calls == 0 and budget.reserved_tool_calls == 1


async def test_reserved_context_survives_crash_and_retry_uses_same_materials(
    database_session_factory,
):
    prepared, packet, store = await prepared_context(database_session_factory)
    parent, claim, clock, tasks, task, _, evidence_store = prepared
    first = await reserve_context(store, prepared, packet)
    assert not (await reserve_context(store, prepared, packet)).created
    changed = packet.model_copy(
        update={"manifest": packet.manifest.model_copy(update={"task_id": uuid4()})}
    )
    with pytest.raises(TaskFactConflict):
        await reserve_context(store, prepared, changed)
    queries = CallBudgetQueries(database_session_factory)
    assert await queries.get_context_manifest(claim.run.id, first.record.id) == packet.manifest
    await parent.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        retry_delay=timedelta(0),
        error=WorkItemError(error_code="crash", message="上下文与预留提交后崩溃"),
    )
    with pytest.raises(TaskFactConflict):
        await reserve_context(store, prepared, packet)
    clock.current += timedelta(seconds=1)
    replacement = await parent.claim_next(
        worker_id="context-retry", occurred_at=clock(), lease_duration=timedelta(seconds=30)
    )
    snapshot = await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
    current = next(t for t in snapshot.rounds[0].tasks if t.id == task.id)
    attempt = await tasks.begin_attempt(
        claim.run.id,
        task.id,
        lease_token=replacement.work_item.lease_token,
        expected_version=current.version,
    )
    second_prepared = (parent, replacement, clock, tasks, current, attempt.attempt, evidence_store)
    second = await reserve_context(store, second_prepared, packet)
    assert first.record.id != second.record.id
    assert await queries.get_context_manifest(claim.run.id, first.record.id) == packet.manifest
    assert await queries.get_context_manifest(claim.run.id, second.record.id) == packet.manifest
    assert await queries.get_model_input(claim.run.id, first.record.id) == packet.model_input
    budget = await queries.get_snapshot(claim.run.id)
    assert budget.reserved_model_calls == 2 and budget.model_usage.unknown_model_calls == 2
    assert next(c for c in budget.calls if c.id == first.record.id).status == "unknown"


async def test_other_run_and_uncommitted_synthesis_are_rejected(database_session_factory):
    prepared, packet, _ = await prepared_context(database_session_factory)
    other, _, _ = await prepared_context(database_session_factory)
    service = TaskContextService(database_session_factory, other[1].run.id)
    with pytest.raises(TaskFactConflict):
        await service.prepare(
            packet.model_input, CallOperation.ANALYZE_LOG, prepared[4].id, model_policy()
        )
    service = TaskContextService(database_session_factory, prepared[1].run.id)
    with pytest.raises(TaskFactConflict) as error:
        await service.prepare(packet.model_input, CallOperation.SYNTHESIZE, None, model_policy())
    assert error.value.code == "context_task_not_ready"


async def test_material_change_after_assembly_is_detected_before_reservation(
    database_session_factory,
):
    prepared, packet, store = await prepared_context(database_session_factory)
    run_id, task_id = prepared[1].run.id, prepared[4].id
    record = await CollaborationQueries(database_session_factory).get_evidence(run_id, task_id)
    evidence = list(record.snapshot.evidence)
    evidence[0] = evidence[0].model_copy(update={"summary": "模拟组装后存储内容损坏"})
    snapshot = record.snapshot.model_copy(update={"evidence": tuple(evidence)})
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(AgentTaskEvidenceRow)
            .where(
                AgentTaskEvidenceRow.task_id == task_id,
            )
            .values(
                snapshot=snapshot.model_dump(mode="json"), content_sha256=evidence_hash(snapshot)
            )
        )
    with pytest.raises(TaskFactConflict) as error:
        await reserve_context(store, prepared, packet)
    assert error.value.code == "context_material_mismatch"
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(run_id)
    assert budget.reserved_model_calls == 0
