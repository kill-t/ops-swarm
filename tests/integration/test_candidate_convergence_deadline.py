"""成功写入后的真实提交过期，事故、事件和槽位必须一并回滚。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.api.test_candidate_convergence_api import (
    active_slot,
    count,
    get,
    marker,
    prepare_settled,
    snapshots,
)
from tests.api.test_candidate_convergence_api import current_fixture_time as current_fixture_time
from tests.api.test_candidate_convergence_api import wait_context as wait_context
from tests.integration.test_candidate_convergence_migration import convergence_values, save_actual

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_commit_after_real_freshness_deadline_rolls_back_all_side_effects(wait_context):
    c = wait_context
    await prepare_settled(c)
    before = await c.incidents.get_incident(c.started.run.incident_id)
    events = await c.incidents.list_events(before.incident.id)
    frozen = await snapshots(c)
    values = await convergence_values(c)
    assert values["content"]["outcome"] == "recovered"
    deadline = datetime.fromisoformat(values["content"]["fresh_until"])
    with pytest.raises(DBAPIError, match="提交时业务样本已过期"):
        async with c.db() as session, session.begin():
            await save_actual(session, values)
            # 不伪造created_at或放宽生产期限；写入后保持事务到真实数据库时钟超期。
            while (remaining := (deadline - datetime.now(UTC)).total_seconds()) > -0.1:
                await session.execute(
                    text("SELECT pg_sleep(:delay)"),
                    {
                        "delay": min(30, remaining + 0.15),
                    },
                )
    assert await c.incidents.get_incident(before.incident.id) == before
    assert await c.incidents.list_events(before.incident.id) == events
    assert await snapshots(c) == frozen
    assert await count(c) == 0 and await marker(c) is None and await active_slot(c) == 1
    assert await c.convergence.converge_next()
    response = await get(c)
    assert response.status_code == 200, response.text
    assert response.json()["record"]["outcome"] == "held_stale"
    assert await c.incidents.get_incident(before.incident.id) == before
    assert await marker(c) is None and await active_slot(c) == 1
