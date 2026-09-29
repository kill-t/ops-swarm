"""摘要组装后再核对业务来源，失败不能消费新的请求额度。"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, text, update

from opsswarm.checkpoints import initialize_checkpoint_schema
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.service import TaskContextService
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.investigation.working_summary.tables import AgentTaskWorkingSummaryRow
from tests.checkpoint_fakes import CountingDataSource
from tests.integration.test_loop_graph import run_workers
from tests.loop_helpers import loop_policy
from tests.summary_context_helpers import SummaryReplies
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def prepared_synthesis(db, checkpoint_url, monkeypatch):
    await initialize_checkpoint_schema(checkpoint_url)
    parent, started = await started_run(
        db,
        workflow_version=WorkflowVersion.V2,
        model_budget=loop_policy(),
        context_policy_version="summary-context-v1",
    )
    clock, reply = TaskClock(BASE_TIME + timedelta(seconds=2)), SummaryReplies(db)
    captured = []
    original = TaskContextService.prepare

    async def pause(
        self,
        supplied,
        operation,
        task_id,
        policy,
        invocation_index=0,
    ):
        packet = await original(
            self,
            supplied,
            operation,
            task_id,
            policy,
            invocation_index,
        )
        if operation == CallOperation.SYNTHESIZE:
            captured.append(packet)
            raise RuntimeError("模拟综合组装完成后预留前进程中断")
        return packet

    with monkeypatch.context() as patch:
        patch.setattr(TaskContextService, "prepare", pause)
        await run_workers(db, checkpoint_url, parent, clock, CountingDataSource(), reply)
    run = await parent.get_run(started.run.id)
    assert run.status == "queued", run.last_error
    assert len(captured) == 1, run.last_error
    clock.current += timedelta(seconds=1)
    claim = await parent.claim_next(
        worker_id="summary-reservation", occurred_at=clock(), lease_duration=timedelta(seconds=30)
    )
    assert claim is not None and claim.run.id == run.id
    return claim, captured[0], CallBudgetStore(db, clock=clock), reply.calls


@pytest.mark.parametrize("corruption", ["payload", "manifest", "scope", "source_call", "missing"])
async def test_summary_reservation_revalidates_exact_input_and_sources(
    database_session_factory, checkpoint_database_url, monkeypatch, corruption
):
    db = database_session_factory
    claim, packet, store, queries = await prepared_synthesis(
        db, checkpoint_database_url, monkeypatch
    )
    run_id = claim.run.id
    before = await queries.get_snapshot(run_id)
    assert before.reserved_model_calls == 6
    payload = json.loads(packet.model_input.messages[1].content)
    summary = payload["working_summaries"][0]
    if corruption == "payload":
        payload["working_summaries"][0]["summary"]["report"]["findings"] = ["篡改原报告"]
        message = packet.model_input.messages[1].model_copy(update={"content": json.dumps(payload)})
        packet = packet.model_copy(
            update={
                "model_input": packet.model_input.model_copy(
                    update={"messages": (packet.model_input.messages[0], message)}
                )
            }
        )
    elif corruption in ("manifest", "scope"):
        materials = list(packet.manifest.materials)
        index = next(i for i, m in enumerate(materials) if m.kind == "task_working_summary")
        change = (
            {"content_sha256": "a" * 64} if corruption == "manifest" else {"material_id": uuid4()}
        )
        materials[index] = materials[index].model_copy(update=change)
        packet = packet.model_copy(
            update={"manifest": packet.manifest.model_copy(update={"materials": tuple(materials)})}
        )
    elif corruption == "source_call":
        async with db() as session, session.begin():
            await session.execute(
                update(CallReservationRow)
                .where(CallReservationRow.id == summary["summary"]["source_call_id"])
                .values(call_result=None)
            )
    else:
        # 故障注入仅在测试事务内绕过摘要不可变触发器，模拟存储材料丢失。
        async with db() as session, session.begin():
            await session.execute(
                text("ALTER TABLE agent_task_working_summaries DISABLE TRIGGER USER")
            )
            await session.execute(
                delete(AgentTaskWorkingSummaryRow).where(
                    AgentTaskWorkingSummaryRow.task_id == summary["task_id"]
                )
            )
            await session.execute(
                text("ALTER TABLE agent_task_working_summaries ENABLE TRIGGER USER")
            )
    with pytest.raises(TaskFactConflict):
        await store.reserve(
            run_id,
            lease_token=claim.work_item.lease_token,
            operation=CallOperation.SYNTHESIZE,
            model_input=packet.model_input,
            context_manifest=packet.manifest,
            prompt_version=packet.manifest.prompt_version,
        )
    after = await queries.get_snapshot(run_id)
    assert after.reserved_model_calls == before.reserved_model_calls
    assert after.reserved_tool_calls == before.reserved_tool_calls


async def test_full_summary_input_over_budget_does_not_reserve(
    database_session_factory, checkpoint_database_url, monkeypatch
):
    db = database_session_factory
    claim, packet, store, queries = await prepared_synthesis(
        db, checkpoint_database_url, monkeypatch
    )
    before = await queries.get_snapshot(claim.run.id)
    budget = loop_policy(
        max_input_tokens_per_call=packet.manifest.allocation.estimated_input_tokens - 1
    )
    # 故障注入模拟预留时额度不足；不引入运行中修改策略的产品接口。
    async with db() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == claim.run.id)
            .values(model_budget=budget.model_dump(mode="json"))
        )
    with pytest.raises(CallBudgetExceeded) as error:
        await store.reserve(
            claim.run.id,
            lease_token=claim.work_item.lease_token,
            operation=CallOperation.SYNTHESIZE,
            model_input=packet.model_input,
            context_manifest=packet.manifest,
            prompt_version=packet.manifest.prompt_version,
        )
    assert error.value.code == "model_input_budget_exceeded"
    after = await queries.get_snapshot(claim.run.id)
    assert after.reserved_model_calls == before.reserved_model_calls == 6
    assert after.reserved_tool_calls == before.reserved_tool_calls == 6
