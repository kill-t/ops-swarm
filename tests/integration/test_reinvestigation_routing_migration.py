"""路由元数据一致性及空库升降级。"""

import asyncio

import pytest
from sqlalchemy import text

from alembic import command
from tests.integration.test_candidate_approval_migration import config

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_0092_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, config())
    try:
        await asyncio.to_thread(command.downgrade, config(), "0091_reflow_review")
        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT to_regclass('investigation_reflow_routes')"))
                is None
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")
    await asyncio.to_thread(command.check, config())


@pytest.mark.parametrize(
    "direction,limits",
    [
        ("evaluate_for_routing", {}),
        ("human_review", {}),
        ("follow_up", {}),
        ("follow_up", {"max_rounds": 2}),
        ("follow_up", {"max_total_tasks": 4}),
    ],
)
async def test_native_base_equals_domain_for_directions_and_original_capacity(
    database_session_factory, direction, limits
):
    import json

    from opsswarm.investigation.coordination.contracts import CoordinationPolicy
    from tests.unit.investigation.test_reinvestigation_routing import (
        compile_for,
        material_source,
        result_for,
    )

    source = material_source(coordination_policy=CoordinationPolicy(**limits))
    review = result_for(source, direction)
    async with database_session_factory() as session:
        native = await session.scalar(
            text("SELECT reflow_routing_base(CAST(:source AS jsonb),CAST(:review AS jsonb))"),
            {"source": source.model_dump_json(), "review": review.proposal.model_dump_json()},
        )
    assert native == json.loads(compile_for(source, review=review).base_decision.model_dump_json())
