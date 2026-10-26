"""有效127执行来源上的原生样本对抗与跨租约提交回滚。"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from opsswarm.investigation.context.hashing import content_hash
from opsswarm.verification.candidate.contracts import RecoveryResultContent
from tests.api.test_candidate_recovery_api import get, prepare
from tests.api.test_release_execution_jobs_api import (
    current_fixture_time as current_fixture_time,
)
from tests.api.test_release_execution_jobs_api import (
    parent_snapshot,
)
from tests.api.test_release_execution_jobs_api import (
    wait_context as wait_context,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_native_sample_attacks_and_commit_after_lease_roll_back(wait_context):
    c = wait_context
    await prepare(c)
    before = await parent_snapshot(c)
    claim = await c.recovery.claim_next(worker_id="native-recovery-review")
    assert claim is not None
    now = c.clock()
    body = dict(
        source="local-order-probe-v1",
        path="/orders/opsswarm-verification-probe",
        requested_at=now.isoformat(),
        observed_at=now.isoformat(),
        http_status=200,
        value=0.0,
        order_id="opsswarm-verification-probe",
    )
    insert = text(
        "INSERT INTO candidate_recovery_samples "
        "(job_id,attempt_id,sequence,observation,error_code,created_at) "
        "VALUES (:job,:attempt,:sequence,CAST(:body AS jsonb),NULL,:now)"
    )
    attacks = [
        ({**body, "source": "frozen"}, {}),
        ({**body, "path": "/health"}, {}),
        ({**body, "http_status": True}, {}),
        ({**body, "value": False}, {}),
        ({**body, "order_id": None}, {}),
        ({**body, "http_status": 500}, {}),
        ({key: value for key, value in body.items() if key != "source"}, {}),
        ({**body, "requested_at": (now - timedelta(seconds=11)).isoformat()}, {}),
        ({**body, "observed_at": (now - timedelta(seconds=11)).isoformat()}, {}),
        (body, {"attempt": uuid4()}),
        (body, {"sequence": 2}),
        (body, {"now": now + timedelta(seconds=5)}),
    ]
    for observation, changes in attacks:
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(
                    insert,
                    {
                        "job": claim.job_id,
                        "attempt": claim.attempt_id,
                        "sequence": 1,
                        "body": json.dumps(observation),
                        "now": now,
                        **changes,
                    },
                )
    assert (await get(c)).json()["samples"] == []
    await c.recovery.save_sample(claim, 1, error_code="probe_unavailable")
    view = (await get(c)).json()
    completed_at = c.clock()
    result = RecoveryResultContent(
        execution_job_id=claim.job_id,
        execution_result_sha256=view["record"]["execution_result_sha256"],
        operation_id=view["record"]["operation_id"],
        outcome="source_failed",
        attempt_id=claim.attempt_id,
        reason_code="probe_source_failed",
        created_at=completed_at,
        parent_advanced=False,
    )
    with pytest.raises(DBAPIError, match="终态事务已越过租约或固定窗口截止"):
        async with c.db() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE candidate_recovery_attempts SET status='failed',finished_at=:now "
                    "WHERE id=:attempt"
                ),
                {"now": completed_at, "attempt": claim.attempt_id},
            )
            await session.execute(
                text(
                    "UPDATE candidate_recovery_jobs SET status='completed',completed_at=:now,"
                    "updated_at=:now,locked_by=NULL,lease_token=NULL,locked_until=NULL "
                    "WHERE id=:job"
                ),
                {"now": completed_at, "job": claim.job_id},
            )
            await session.execute(
                text(
                    "INSERT INTO candidate_recovery_results"
                    "(job_id,content,content_sha256,created_at) "
                    "VALUES (:job,CAST(:body AS jsonb),:digest,:now)"
                ),
                {
                    "job": claim.job_id,
                    "body": result.model_dump_json(),
                    "digest": content_hash(result),
                    "now": completed_at,
                },
            )
            delay = (claim.lease_expires_at - c.clock()).total_seconds() + 0.1
            assert 0 < delay < 30
            await session.execute(text("SELECT pg_sleep(:delay)"), {"delay": delay})
    after = (await get(c)).json()
    assert after == view
    assert after["attempts"][0]["status"] == "running"
    assert after["result"] is None
    assert await parent_snapshot(c) == before
