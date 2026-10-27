"""过滤失格扫描材料后，有限扫描仍可处理后续真实合格运行。"""

import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from opsswarm.investigation.settlement.eligibility import eligible_recoveries
from tests.api.test_candidate_recovery_api import current_fixture_time as current_fixture_time
from tests.api.test_candidate_recovery_api import prepare
from tests.api.test_candidate_recovery_api import wait_context as wait_context
from tests.api.test_candidate_settlement_api import bind, get, recover

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_eight_permanently_ineligible_rows_cannot_starve_the_ninth_real_recovery(
    wait_context,
):
    c = wait_context
    execution = await prepare(c)
    await recover(c)
    await bind(c)
    actual = UUID(execution["record"]["id"])
    # 仅用于选取器的最小对抗材料：不构造虚假完整审批/证据链，也不称为合法收尾来源。
    # 它们的核验版本2不等于实际最高版本1，必须在加载业务历史前被过滤。
    async with c.db() as session, session.begin():
        await session.execute(text("SET LOCAL session_replication_role='replica'"))
        for number in range(8):
            request_id, execution_id, operation_id = uuid4(), uuid4(), uuid4()
            patches = (
                (
                    "candidate_release_execution_requests",
                    UUID(execution["record"]["request_id"]),
                    {
                        "id": str(request_id),
                        "approval_decision_id": str(uuid4()),
                        "operation_id": str(operation_id),
                        "authorization_id": str(uuid4()),
                        "idempotency_key": f"stale-request-scan-{number}",
                        "verification_version": 2,
                    },
                ),
                (
                    "candidate_release_execution_jobs",
                    actual,
                    {
                        "id": str(execution_id),
                        "request_id": str(request_id),
                        "operation_id": str(operation_id),
                        "authorization_id": str(uuid4()),
                        "idempotency_key": f"stale-execution-scan-{number}",
                        "verification_version": 2,
                    },
                ),
                (
                    "candidate_recovery_jobs",
                    actual,
                    {
                        "id": str(execution_id),
                        "operation_id": str(operation_id),
                        "verification_version": 2,
                        "completed_at": (c.clock() - timedelta(days=1, seconds=number)).isoformat(),
                    },
                ),
            )
            for table, source, patch in patches:
                await session.execute(
                    text(
                        f"INSERT INTO {table} SELECT (jsonb_populate_record(NULL::{table}, "
                        f"to_jsonb(original)||CAST(:patch AS jsonb))).* FROM {table} original "
                        "WHERE id=:source"
                    ),
                    {"patch": json.dumps(patch), "source": source},
                )
        await session.execute(text("SET LOCAL session_replication_role='origin'"))
    async with c.db() as session:
        assert (
            len(
                tuple(
                    await session.scalars(
                        select(text("id")).select_from(text("candidate_recovery_jobs"))
                    )
                )
            )
            == 9
        )
        assert [row.id for row in await session.scalars(eligible_recoveries())] == [actual]
    assert await c.settlement.settle_next()
    assert (await get(c)).json()["record"]["recovery_job_id"] == str(actual)
    assert not await c.settlement.settle_next()
