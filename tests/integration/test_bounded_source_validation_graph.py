"""真实三轮来源图的 SQL 上界、经验隔离和跨租约 checkpoint 恢复。"""

import asyncio
import json
from time import perf_counter

import httpx2
import pytest
from sqlalchemy import event

import opsswarm.experience.context_store as experience_module
from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.queries import CallBudgetQueries
from tests.api.test_coordination_experience_api import reclaim, start
from tests.api.test_experience_lifecycle_api import disable, history
from tests.api.test_experience_lifecycle_api import experience_context as experience_fixture
from tests.integration.test_dynamic_first_round_graph import (
    RecordingFrozenSource,
    processor_context,
)
from tests.integration.test_dynamic_third_round_routing_graph import (
    ThirdRoundRoutingReplies,
    routing_factory,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
experience_context = experience_fixture


@pytest.mark.parametrize("enabled", [False, True])
async def test_three_round_source_validation_is_bounded_and_recovers_without_external_calls(
    experience_context, database_session_factory, checkpoint_database_url, monkeypatch, enabled
):
    entry, _ = await history(experience_context, database_session_factory)
    _, api, clock = experience_context
    store, claim = await start(experience_context, database_session_factory, enabled=enabled)
    await initialize_checkpoint_schema(checkpoint_database_url)
    replies = ThirdRoundRoutingReplies(database_session_factory, third_route_case="candidate")
    inputs = []
    counts = {"sql": 0, "gates": 0}

    def count_sql(conn, cursor, statement, parameters, context, executemany):
        counts["sql"] += 1
        counts["gates"] += "chosen_entries AS" in statement

    async def reply(request):
        inputs.append(json.loads(json.loads(request.content)["messages"][1]["content"]))
        return await replies(request)

    engine = database_session_factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", count_sql)
    started = perf_counter()
    try:
        async with (
            asyncio.timeout(180),
            httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as model,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = routing_factory(
                database_session_factory, RecordingFrozenSource(claim.request), model, saver, clock
            )(processor_context(claim))
            result = await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
            assert result.third_round_review_route.decision.route == "candidate_evaluation"
            investigation_seconds = perf_counter() - started
            investigation_queries = dict(counts)
            assert (await disable(api, entry)).status_code == 201
            claim = await reclaim(experience_context, store, claim)

            async def forbidden(*args, **kwargs):
                pytest.fail("恢复不能重新检索当前有效经验")

            monkeypatch.setattr(experience_module, "search_in_session", forbidden)
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(lambda _: pytest.fail("完成图恢复不能重调 SDK"))
            ) as recovery_model:
                recovered = routing_factory(
                    database_session_factory,
                    RecordingFrozenSource(claim.request),
                    recovery_model,
                    saver,
                    clock,
                )(processor_context(claim))
                assert await recovered.resume(thread_id=claim.run.graph_thread_id) == result
    finally:
        event.remove(engine, "before_cursor_execute", count_sql)
    elapsed = perf_counter() - started
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert budget.reserved_model_calls == 13 and budget.reserved_tool_calls == 6
    assert sum("historical_experience" in data for data in inputs) == int(enabled)
    print(
        json.dumps(
            {
                "experience": enabled,
                "investigation_seconds": round(investigation_seconds, 2),
                "investigation_queries": investigation_queries,
                "total_seconds": round(elapsed, 2),
                "total_queries": counts,
            }
        )
    )

    assert 0 < counts["gates"] < counts["sql"] < 25000
    assert elapsed < 180
