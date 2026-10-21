"""真实来源、PostgreSQL 与假 SDK 验证首轮协调经验的冻结和恢复。"""

import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

import opsswarm.experience.context_store as context_module
from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.experience.context_tables import ExperienceContextRow
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.coordination.context import CoordinationContextService
from opsswarm.investigation.coordination.contracts import CoordinationPolicy
from opsswarm.investigation.coordination.persistence import CoordinationPlanStore
from opsswarm.investigation.coordination.planner import CoordinationPlanner
from opsswarm.investigation.models import EvidenceKind, WorkItemError
from opsswarm.investigation.persistence.repository import InvestigationStore
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.api.test_control_plane_api import BASE_TIME, alert_payload
from tests.api.test_experience_lifecycle_api import (
    disable,
    history,
)
from tests.api.test_experience_lifecycle_api import (
    experience_context as experience_fixture,
)
from tests.api.test_experience_replacement_api import replace
from tests.identity_helpers import auth_header
from tests.integration.test_coordination_planner import proposal
from tests.integration.test_dynamic_first_round_graph import (
    RecordingFrozenSource,
    processor_context,
)
from tests.integration.test_dynamic_review_graph import ReviewReplies
from tests.integration.test_dynamic_third_round_routing_graph import (
    routing_factory,
)
from tests.loop_helpers import loop_policy
from tests.model_budget_helpers import sdk_model, sdk_response

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
experience_context = experience_fixture


def policy(**changes):
    return loop_policy(
        **{
            "max_input_tokens_per_call": 50000,
            "max_input_tokens": 800000,
            "max_output_tokens": 50000,
            **changes,
        }
    )


async def start(context, factory, *, enabled=True, budget=None):
    _, client, clock = context
    clock.value = BASE_TIME + timedelta(seconds=1000)
    ingested = await client.post(
        "/api/v1/alerts",
        json={**alert_payload(f"coord-experience-{uuid4()}"), "fingerprint": str(uuid4())},
        headers=auth_header("alert_source"),
    )
    incident = ingested.json()["incident"]
    body = {
        "expected_version": 1,
        "reason": "显式验证协调经验策略",
        "mode": "multi_agent",
        "workflow_version": "v3",
        "model_budget": (budget or policy()).model_dump(mode="json"),
        "call_budget": {"max_model_calls": 16},
    }
    if enabled:
        body["coordination_experience_policy_version"] = "coordination-experience-v1"
    response = await client.post(
        f"/api/v1/incidents/{incident['id']}/actions/start-investigation",
        json=body,
        headers=auth_header("engineer"),
    )
    assert response.status_code == 200, response.text
    run = response.json()["investigation"]
    assert run["coordination_experience_policy_version"] == (
        "coordination-experience-v1" if enabled else None
    )
    store = InvestigationStore(factory)
    claim = await store.claim_next(
        worker_id="coordination-experience-worker",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert claim is not None and str(claim.run.id) == run["id"]
    return store, claim


def planner(factory, claim, clock, client):
    return CoordinationPlanner(
        factory,
        run_id=claim.run.id,
        lease_token=claim.work_item.lease_token,
        model=sdk_model(client),
        clock=clock,
    )


def arguments():
    return {
        "policy": CoordinationPolicy(),
        "observation_cutoff_at": BASE_TIME,
        "evidence_snapshot_id": "coordination-experience-test-v1",
    }


async def rows(factory, model):
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def prepared(context, factory, claim):
    _, _, clock = context
    await CoordinationPlanStore(factory, clock=clock).fix_model_policy(
        claim.run.id, lease_token=claim.work_item.lease_token, policy=CoordinationPolicy()
    )
    return await CoordinationContextService(factory, claim.run.id, clock=clock).prepare(
        observation_cutoff_at=BASE_TIME, evidence_snapshot_id="coordination-experience-test-v1"
    )


async def reserve(context, factory, claim, packet):
    return await CallBudgetStore(factory, clock=context[2]).reserve(
        claim.run.id,
        lease_token=claim.work_item.lease_token,
        operation=CallOperation.COORDINATE,
        model_input=packet.model_input,
        prompt_version=packet.manifest.prompt_version,
        context_manifest=packet.manifest,
    )


async def reclaim(context, store, claim):
    clock = context[2]
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="test_crash", message="模拟协调提交窗口中断"),
        retry_delay=timedelta(0),
    )
    clock.advance(1)
    replacement = await store.claim_next(
        worker_id="replacement-coordinator",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None and replacement.run.id == claim.run.id
    return replacement


@pytest.mark.parametrize("invalid", ["null", "version", "v2", "live", "context", "profile"])
async def test_start_rejects_unsupported_experience_policy_combinations(
    experience_context, invalid
):
    body = {
        "expected_version": 1,
        "reason": "验证经验启用边界",
        "mode": "multi_agent",
        "workflow_version": "v3",
        "model_budget": policy().model_dump(mode="json"),
        "coordination_experience_policy_version": "coordination-experience-v1",
    }
    if invalid == "null":
        body["coordination_experience_policy_version"] = None
    elif invalid == "version":
        body["coordination_experience_policy_version"] = "unknown-v1"
    elif invalid == "v2":
        body["workflow_version"] = "v2"
    elif invalid == "live":
        body["data_source_mode"] = "live"
    elif invalid == "context":
        body["context_policy_version"] = "task-context-v1"
    else:
        body["model_budget"]["profile"]["structured_output_method"] = "json_schema"
    response = await experience_context[1].post(
        f"/api/v1/incidents/{uuid4()}/actions/start-investigation",
        json=body,
        headers=auth_header("engineer"),
    )
    assert response.status_code == 422, response.text


async def test_actual_model_receives_bounded_exact_historical_materials_and_manifest(
    experience_context,
    database_session_factory,
):
    entries = [
        (await history(experience_context, database_session_factory, number=i))[0] for i in range(4)
    ]
    _, api, clock = experience_context
    _, claim = await start(experience_context, database_session_factory)
    sent = []

    def reply(request):
        payload = json.loads(request.content)
        sent.append(json.loads(payload["messages"][1]["content"])["historical_experience"])
        return sdk_response(payload, proposal().model_dump(mode="json"))

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as model:
        plan, created, reused = await planner(
            database_session_factory, claim, clock, model
        ).create_first_plan(**arguments())
    assert created and not reused and len(sent) == 1
    assert [item["entry"]["id"] for item in sent[0]["items"]] == [e["id"] for e in entries[:0:-1]]
    assert sent[0]["items"][0]["entry"]["content"] == entries[3]["content"]
    response = await api.get(
        f"/api/v1/investigations/{claim.run.id}/calls/{plan.source_call_id}/context-manifest",
        headers=auth_header("viewer"),
    )
    # 同时核对统一预算接口与业务清单，避免仅检查假模型消息。
    assert response.status_code == 200, response.text
    queries = CallBudgetQueries(database_session_factory)
    manifest = await queries.get_context_manifest(claim.run.id, plan.source_call_id)
    assert manifest.version == "coordination-experience-v1"
    assert [str(ref.material_id) for ref in manifest.materials[3:]] == [
        e["id"] for e in entries[:0:-1]
    ]
    assert manifest.materials[3].content_sha256 == entries[3]["content_hash"]
    assert all(not ref.evidence_ids for ref in manifest.materials[2:])
    assert await rows(database_session_factory, ExperienceContextRow) == 1
    async with database_session_factory() as session:
        saved = await session.get(ExperienceContextRow, claim.run.id)
        assert saved.first_call_id == plan.source_call_id


@pytest.mark.parametrize("enabled", [False, True])
async def test_empty_choice_is_frozen_only_for_explicit_new_strategy(
    experience_context,
    database_session_factory,
    enabled,
):
    _, _, clock = experience_context
    _, claim = await start(experience_context, database_session_factory, enabled=enabled)
    packet = await prepared(experience_context, database_session_factory, claim)
    assert await rows(database_session_factory, ExperienceContextRow) == 0
    clock.advance(1)
    await reserve(experience_context, database_session_factory, claim, packet)
    assert await rows(database_session_factory, ExperienceContextRow) == int(enabled)
    if enabled:
        await history(experience_context, database_session_factory)
        clock.value = BASE_TIME + timedelta(seconds=1001)
        again = await prepared(experience_context, database_session_factory, claim)
        assert again == packet
        assert (
            json.loads(again.model_input.messages[1].content)["historical_experience"]["items"]
            == []
        )
    else:
        assert packet.model_input.version == "coordination-model-input-v1"
        assert len(packet.manifest.materials) == 2


@pytest.mark.parametrize("retirement", ["disable", "replace"])
@pytest.mark.parametrize("window", ["before_plan", "after_plan"])
async def test_successful_call_and_plan_recovery_keep_frozen_material_after_retirement(
    experience_context,
    database_session_factory,
    monkeypatch,
    retirement,
    window,
):
    old, _ = await history(experience_context, database_session_factory)
    new, _ = await history(experience_context, database_session_factory, number=1)
    _, api, clock = experience_context
    store, claim = await start(experience_context, database_session_factory)
    calls = []

    def reply(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return sdk_response(payload, proposal().model_dump(mode="json"))

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as model:
        first = planner(database_session_factory, claim, clock, model)
        if window == "before_plan":
            called = await first.propose_first_round(**arguments())
            first_id = called.call_id
        else:
            original, _, _ = await first.create_first_plan(**arguments())
            first_id = original.source_call_id
    response = await disable(api, old) if retirement == "disable" else await replace(api, old, new)
    assert response.status_code == 201
    claim = await reclaim(experience_context, store, claim)

    async def forbidden(*args, **kwargs):
        pytest.fail("历史协调恢复不应重新查询当前有效经验")

    monkeypatch.setattr(context_module, "search_in_session", forbidden)
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: pytest.fail("成功协调恢复不应重调模型"))
    ) as model:
        recovered, created, reused = await planner(
            database_session_factory, claim, clock, model
        ).create_first_plan(**arguments())
    assert created is (window == "before_plan") and reused
    assert recovered.source_call_id == first_id and len(calls) == 1
    assert await rows(database_session_factory, ExperienceContextRow) == 1


@pytest.mark.parametrize("status", ["failed", "unknown"])
async def test_failed_attempt_retry_keeps_selection_and_binds_new_actual_call(
    experience_context,
    database_session_factory,
    status,
):
    entry, _ = await history(experience_context, database_session_factory)
    _, api, clock = experience_context
    store, claim = await start(experience_context, database_session_factory)
    packet = await prepared(experience_context, database_session_factory, claim)
    grant = await reserve(experience_context, database_session_factory, claim, packet)
    await CallBudgetStore(database_session_factory, clock=clock).settle(
        claim.run.id,
        grant.record.id,
        lease_token=claim.work_item.lease_token,
        status=status,
        error_code="operation_failed",
    )
    assert (await disable(api, entry)).status_code == 201
    claim = await reclaim(experience_context, store, claim)
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: sdk_response(
                json.loads(request.content), proposal().model_dump(mode="json")
            )
        )
    ) as model:
        plan, _, reused = await planner(
            database_session_factory, claim, clock, model
        ).create_first_plan(**arguments())
    assert not reused and plan.source_call_id != grant.record.id
    async with database_session_factory() as session:
        saved = await session.get(ExperienceContextRow, claim.run.id)
        assert saved.first_call_id == grant.record.id
    calls = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert calls.reserved_model_calls == 2
    current_manifest = await CallBudgetQueries(database_session_factory).get_context_manifest(
        claim.run.id, plan.source_call_id
    )
    assert current_manifest.materials[2:] == packet.manifest.materials[2:]


async def test_retirement_between_prepare_and_reserve_rejects_without_frozen_or_call_fact(
    experience_context,
    database_session_factory,
):
    entry, _ = await history(experience_context, database_session_factory)
    _, api, _ = experience_context
    _, claim = await start(experience_context, database_session_factory)
    packet = await prepared(experience_context, database_session_factory, claim)
    assert (await disable(api, entry)).status_code == 201
    with pytest.raises(TaskFactConflict):
        await reserve(experience_context, database_session_factory, claim, packet)
    assert await rows(database_session_factory, ExperienceContextRow) == 0
    assert await rows(database_session_factory, CallReservationRow) == 0
    fresh = await prepared(experience_context, database_session_factory, claim)
    await reserve(experience_context, database_session_factory, claim, fresh)


@pytest.mark.parametrize("failure", ["budget", "lease", "manifest"])
async def test_failed_reservation_never_commits_an_orphan_selection(
    experience_context,
    database_session_factory,
    failure,
):
    await history(experience_context, database_session_factory)
    _, _, clock = experience_context
    _, claim = await start(
        experience_context,
        database_session_factory,
        budget=policy(max_input_tokens=1) if failure == "budget" else None,
    )
    packet = await prepared(experience_context, database_session_factory, claim)
    if failure == "lease":
        clock.advance(31)
    elif failure == "manifest":
        changed = packet.manifest.model_copy(update={"materials": packet.manifest.materials[:3]})
        packet = packet.model_copy(update={"manifest": changed})
    with pytest.raises((CallBudgetExceeded, TaskFactConflict)):
        await reserve(experience_context, database_session_factory, claim, packet)
    assert await rows(database_session_factory, ExperienceContextRow) == 0
    assert await rows(database_session_factory, CallReservationRow) == 0


@pytest.mark.parametrize(
    "table", ["investigation_experience_contexts", "investigation_call_reservations"]
)
async def test_sql_failure_rolls_back_snapshot_and_reservation_together(
    experience_context,
    database_session_factory,
    table,
):
    await history(experience_context, database_session_factory)
    _, claim = await start(experience_context, database_session_factory)
    packet = await prepared(experience_context, database_session_factory, claim)
    async with database_session_factory() as session, session.begin():
        await session.execute(
            text(f"""
            CREATE FUNCTION test_context_failure() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION '测试经验预算事务失败'; END; $$ LANGUAGE plpgsql;
            CREATE TRIGGER test_context_failure_insert AFTER INSERT ON {table}
                FOR EACH ROW EXECUTE FUNCTION test_context_failure();
        """)
        )
    try:
        with pytest.raises(DBAPIError):
            await reserve(experience_context, database_session_factory, claim, packet)
        assert await rows(database_session_factory, ExperienceContextRow) == 0
        assert await rows(database_session_factory, CallReservationRow) == 0
    finally:
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    f"DROP TRIGGER test_context_failure_insert ON {table}; "
                    "DROP FUNCTION test_context_failure();"
                )
            )
    await reserve(experience_context, database_session_factory, claim, packet)


async def test_graph_and_checkpoint_keep_role_isolation_and_frozen_experience(
    experience_context,
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
):
    entry, _ = await history(experience_context, database_session_factory)
    _, api, clock = experience_context
    store, claim = await start(experience_context, database_session_factory)
    await initialize_checkpoint_schema(checkpoint_database_url)
    replies = ReviewReplies(database_session_factory, tuple(EvidenceKind), route_case="candidate")
    inputs = []

    async def reply(request):
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        inputs.append(data)
        return await replies(request)

    async with (
        asyncio.timeout(120),
        httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = routing_factory(
            database_session_factory, RecordingFrozenSource(claim.request), client, saver, clock
        )(processor_context(claim))
        result = await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        assert result.review_route.decision.route == "candidate_evaluation"
        assert result.follow_up_plan is None and result.third_round_plan is None
        checkpoint = await saver.aget_tuple(
            {"configurable": {"thread_id": claim.run.graph_thread_id}}
        )
        assert checkpoint.metadata["graph_version"] == "dynamic-investigation-graph-v15"
        assert (await disable(api, entry)).status_code == 201
        claim = await reclaim(experience_context, store, claim)

        async def forbidden(*args, **kwargs):
            pytest.fail("checkpoint 恢复不能重查当前有效经验")

        monkeypatch.setattr(context_module, "search_in_session", forbidden)
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda _: pytest.fail("完成图恢复不能重调模型"))
        ) as recovery_client:
            recovered_graph = routing_factory(
                database_session_factory,
                RecordingFrozenSource(claim.request),
                recovery_client,
                saver,
                clock,
            )(processor_context(claim))
            assert await recovered_graph.resume(thread_id=claim.run.graph_thread_id) == result
    with_history = [item for item in inputs if "historical_experience" in item]
    assert len(with_history) == 1
    assert with_history[0]["historical_experience"]["items"][0]["entry"]["id"] == entry["id"]
    queries = CallBudgetQueries(database_session_factory)
    budget = await queries.get_snapshot(claim.run.id)
    assert budget.reserved_model_calls == 9 and budget.reserved_tool_calls == 6
    coordinate = next(item for item in budget.calls if item.operation == CallOperation.COORDINATE)
    saved_input = await queries.get_model_input(claim.run.id, coordinate.id)
    assert saved_input.version == "coordination-experience-input-v1"
