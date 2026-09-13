"""真实 PostgreSQL 与假 SDK 验证协调调用、来源绑定和崩溃恢复。"""

import json
from datetime import timedelta

import httpx2
import pytest
from sqlalchemy import func, select, update

from opsswarm.investigation.budget.contracts import CallBudgetPolicy, CallOperation
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.coordination.contracts import (
    CoordinationModelInput,
    CoordinationModelResult,
    CoordinationPolicy,
    CoordinationProposal,
    ProposedTask,
)
from opsswarm.investigation.coordination.planner import CoordinationPlanner
from opsswarm.investigation.coordination.queries import CoordinationPlanQueries
from opsswarm.investigation.models import WorkItemError
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.tables import InvestigationRoundRow
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.model_budget_helpers import model_policy, sdk_model, sdk_response
from tests.task_facts_helpers import BASE_TIME, TaskClock, claimed_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def function_policy():
    policy = model_policy()
    return policy.model_copy(
        update={
            "profile": policy.profile.model_copy(
                update={"structured_output_method": "function_calling"}
            )
        }
    )


def proposal(*, goal: str = "核对关键日志和变更") -> CoordinationProposal:
    return CoordinationProposal(
        round_number=1,
        goal=goal,
        tasks=(
            ProposedTask(
                task_key="log",
                role="log",
                goal="检查异常日志模式",
                lookback_seconds=600,
            ),
            ProposedTask(
                task_key="change",
                role="change",
                goal="核对近期发布",
                lookback_seconds=3600,
            ),
        ),
    )


async def prepared(database_session_factory):
    return await claimed_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        call_budget=CallBudgetPolicy(),
        model_budget=function_policy(),
    )


async def test_model_proposal_uses_shared_budget_and_becomes_plan_source(
    database_session_factory,
):
    _, claim = await prepared(database_session_factory)
    requests = []

    def reply(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert "tools" in payload and payload["tool_choice"]["function"]["name"]
        return sdk_response(payload, proposal().model_dump(mode="json"))

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client:
        plan, created, reused = await CoordinationPlanner(
            database_session_factory,
            run_id=claim.run.id,
            lease_token=claim.work_item.lease_token,
            model=sdk_model(client),
            clock=TaskClock(),
        ).create_first_plan(
            policy=CoordinationPolicy(),
            observation_cutoff_at=BASE_TIME,
            evidence_snapshot_id="dynamic-snapshot-v1",
        )

    assert created is True and reused is False and len(requests) == 1
    assert plan.source_call_id is not None and plan.plan.proposal == proposal()
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert len(budget.calls) == 1
    assert budget.calls[0].operation is CallOperation.COORDINATE
    assert budget.calls[0].status == "succeeded"
    assert budget.calls[0].model_usage is not None
    saved_input = await CallBudgetQueries(database_session_factory).get_model_input(
        claim.run.id, plan.source_call_id
    )
    assert isinstance(saved_input, CoordinationModelInput)
    interaction = await CallBudgetQueries(database_session_factory).get_interaction(
        claim.run.id, plan.source_call_id
    )
    assert interaction.result == CoordinationModelResult(proposal=proposal())
    manifest = await CallBudgetQueries(database_session_factory).get_context_manifest(
        claim.run.id, plan.source_call_id
    )
    assert manifest is not None and manifest.role == "coordinator"
    assert manifest.workflow_version == "v2"
    assert tuple(item.kind for item in manifest.materials) == (
        "incident_input",
        "coordination_policy",
    )


async def test_replacement_worker_reuses_successful_proposal_without_second_request(
    database_session_factory,
):
    parent, claim = await prepared(database_session_factory)
    clock = TaskClock()
    called = []

    def first_reply(request):
        payload = json.loads(request.content)
        called.append(payload)
        return sdk_response(payload, proposal().model_dump(mode="json"))

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(first_reply)) as client:
        first = await CoordinationPlanner(
            database_session_factory,
            run_id=claim.run.id,
            lease_token=claim.work_item.lease_token,
            model=sdk_model(client),
            clock=clock,
        ).propose_first_round(
            policy=CoordinationPolicy(),
            observation_cutoff_at=BASE_TIME,
            evidence_snapshot_id="dynamic-snapshot-v1",
        )
    await parent.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="计划提交前崩溃"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await parent.claim_next(
        worker_id="replacement",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
    )
    assert replacement is not None

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: pytest.fail("不应再次调用模型"))
    ) as client:
        plan, created, reused = await CoordinationPlanner(
            database_session_factory,
            run_id=claim.run.id,
            lease_token=replacement.work_item.lease_token,
            model=sdk_model(client),
            clock=clock,
        ).create_first_plan(
            policy=CoordinationPolicy(),
            observation_cutoff_at=BASE_TIME,
            evidence_snapshot_id="dynamic-snapshot-v1",
        )
    assert created is True and reused is True
    assert plan.source_call_id == first.call_id and len(called) == 1
    async with database_session_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(CallReservationRow)
                .where(CallReservationRow.operation == CallOperation.COORDINATE.value)
            )
            == 1
        )


async def test_replacement_worker_returns_committed_plan_without_second_request(
    database_session_factory,
):
    parent, claim = await prepared(database_session_factory)
    clock = TaskClock()
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: sdk_response(
                json.loads(request.content), proposal().model_dump(mode="json")
            )
        )
    ) as client:
        original, created, _ = await CoordinationPlanner(
            database_session_factory,
            run_id=claim.run.id,
            lease_token=claim.work_item.lease_token,
            model=sdk_model(client),
            clock=clock,
        ).create_first_plan(
            policy=CoordinationPolicy(),
            observation_cutoff_at=BASE_TIME,
            evidence_snapshot_id="dynamic-snapshot-v1",
        )
    assert created is True
    await parent.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="计划提交后崩溃"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await parent.claim_next(
        worker_id="replacement",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
    )
    assert replacement is not None
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: pytest.fail("不应再次调用模型"))
    ) as client:
        recovered, created, reused = await CoordinationPlanner(
            database_session_factory,
            run_id=claim.run.id,
            lease_token=replacement.work_item.lease_token,
            model=sdk_model(client),
            clock=clock,
        ).create_first_plan(
            policy=CoordinationPolicy(),
            observation_cutoff_at=BASE_TIME,
            evidence_snapshot_id="dynamic-snapshot-v1",
        )
    assert recovered == original and created is False and reused is True


async def test_business_invalid_proposal_is_failed_and_does_not_poison_retry(
    database_session_factory,
):
    parent, claim = await prepared(database_session_factory)
    clock = TaskClock()
    invalid = proposal().model_copy(
        update={
            "tasks": (
                ProposedTask(
                    task_key="metric",
                    role="log",
                    goal="越权键名",
                    lookback_seconds=600,
                ),
            )
        }
    )
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: sdk_response(
                json.loads(request.content), invalid.model_dump(mode="json")
            )
        )
    ) as client:
        with pytest.raises(TaskFactConflict) as caught:
            await CoordinationPlanner(
                database_session_factory,
                run_id=claim.run.id,
                lease_token=claim.work_item.lease_token,
                model=sdk_model(client),
                clock=clock,
            ).create_first_plan(
                policy=CoordinationPolicy(),
                observation_cutoff_at=BASE_TIME,
                evidence_snapshot_id="dynamic-snapshot-v1",
            )
    assert caught.value.code == "coordination_task_key_invalid"
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert len(budget.calls) == 1 and budget.calls[0].status == "failed"
    assert await CoordinationPlanQueries(database_session_factory).list_plans(claim.run.id) == ()

    await parent.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="invalid_plan", message="模型提案未通过规则"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await parent.claim_next(
        worker_id="replacement",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
    )
    assert replacement is not None
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: sdk_response(
                json.loads(request.content), proposal().model_dump(mode="json")
            )
        )
    ) as client:
        plan, created, reused = await CoordinationPlanner(
            database_session_factory,
            run_id=claim.run.id,
            lease_token=replacement.work_item.lease_token,
            model=sdk_model(client),
            clock=clock,
        ).create_first_plan(
            policy=CoordinationPolicy(),
            observation_cutoff_at=BASE_TIME,
            evidence_snapshot_id="dynamic-snapshot-v1",
        )
    assert plan.source_call_id is not None and created is True and reused is False


async def test_unsupported_run_leaves_no_policy_and_coordinate_index_is_fixed(
    database_session_factory,
):
    _, old_claim = await claimed_run(database_session_factory)
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: pytest.fail("不应调用模型"))
    ) as client:
        with pytest.raises(TaskFactConflict) as caught:
            await CoordinationPlanner(
                database_session_factory,
                run_id=old_claim.run.id,
                lease_token=old_claim.work_item.lease_token,
                model=sdk_model(client),
                clock=TaskClock(),
            ).propose_first_round(
                policy=CoordinationPolicy(),
                observation_cutoff_at=BASE_TIME,
                evidence_snapshot_id="dynamic-snapshot-v1",
            )
    assert caught.value.code == "coordination_context_not_supported"
    assert (
        await CoordinationPlanQueries(database_session_factory).get_policy(old_claim.run.id) is None
    )

    _, claim = await prepared(database_session_factory)
    with pytest.raises(TaskFactConflict) as caught:
        await CallBudgetStore(database_session_factory, clock=TaskClock()).reserve(
            claim.run.id,
            lease_token=claim.work_item.lease_token,
            operation=CallOperation.COORDINATE,
            invocation_index=1,
        )
    assert caught.value.code == "coordination_invocation_invalid"


async def test_plan_query_rejects_tampered_coordination_result(database_session_factory):
    _, claim = await prepared(database_session_factory)
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: sdk_response(
                json.loads(request.content), proposal().model_dump(mode="json")
            )
        )
    ) as client:
        plan, _, _ = await CoordinationPlanner(
            database_session_factory,
            run_id=claim.run.id,
            lease_token=claim.work_item.lease_token,
            model=sdk_model(client),
            clock=TaskClock(),
        ).create_first_plan(
            policy=CoordinationPolicy(),
            observation_cutoff_at=BASE_TIME,
            evidence_snapshot_id="dynamic-snapshot-v1",
        )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(CallReservationRow)
            .where(CallReservationRow.id == plan.source_call_id)
            .values(
                call_result=CoordinationModelResult(
                    proposal=proposal(goal="被改写的协调结论")
                ).model_dump(mode="json")
            )
        )
    with pytest.raises(TaskFactConflict) as caught:
        await CoordinationPlanQueries(database_session_factory).list_plans(claim.run.id)
    assert caught.value.code == "coordination_plan_integrity_violation"
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(InvestigationRoundRow)) == 1
