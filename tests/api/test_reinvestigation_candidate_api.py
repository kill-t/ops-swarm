"""单条真实回流链验证候选消费、原账本、恢复和原生保护。"""

import asyncio
import copy
import json
import sys
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.budget.settlement import close_open_reservations
from opsswarm.investigation.candidate.output import CandidateModelResult, CandidateProposal
from opsswarm.investigation.handoff.persistence import validated_handoff
from opsswarm.investigation.handoff.tables import InvestigationTerminalHandoffRow
from opsswarm.investigation.reinvestigation_candidate.contracts import ReflowCandidateError
from opsswarm.investigation.reinvestigation_candidate.job_store import ReflowCandidateJobStore
from opsswarm.investigation.reinvestigation_candidate.ledger import ReflowCandidateLedger
from opsswarm.investigation.reinvestigation_candidate.queries import ReflowCandidateQueries
from opsswarm.investigation.reinvestigation_candidate.worker import ReflowCandidateWorker
from tests.api import test_reinvestigation_routing_api as routing
from tests.api.test_reinvestigation_routing_api import current_fixture_time as current_fixture_time
from tests.api.test_reinvestigation_routing_api import (
    initial_reflow_capacity as initial_reflow_capacity,
)
from tests.api.test_reinvestigation_routing_api import wait_context as wait_context
from tests.identity_helpers import auth_header
from tests.integration.test_candidate_approval_migration import config

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class Model:
    def __init__(self, hook=None):
        self.calls = []
        self.hook = hook
        self.usage = ReportedModelUsage(input_tokens=80, output_tokens=20, total_tokens=100)

    async def invoke(self, model_input):
        self.calls.append(model_input)
        source = json.loads(model_input.messages[1].content)["source"]
        if self.hook:
            await self.hook()
        return CandidateModelResult(
            proposal=CandidateProposal(
                status="candidate",
                action_type="rollback_release",
                rationale="依据本次新变更评估回滚能力，不证明根因",
                evidence_ids=source["review_result"]["proposal"][
                    "accepted_supporting_evidence_ids"
                ],
                preconditions=("必须经过新的规划、核验和可信审批",),
                unresolved_questions=source["routing"]["unresolved_questions"],
            )
        )


def worker(c, model):
    return ReflowCandidateWorker(
        c.reflow_candidate_store,
        c.reflow_candidate_ledger,
        model_factory=lambda _: model,
        worker_id="candidate-test-138",
    )


async def get(c, headers=None):
    return await c.client.get(
        c.reflow_candidate_base, headers=auth_header("viewer") if headers is None else headers
    )


async def temporary_alert(session, c):
    await session.execute(
        text(
            "INSERT INTO incident_events(incident_id,event_type,actor_type,to_status,"
            "reason,incident_version,payload,occurred_at) SELECT id,"
            "'incident.alert_attached','system',status,'模拟候选恢复前来源改变',version,"
            "'{}'::jsonb,clock_timestamp() FROM incidents WHERE id=:id"
        ),
        {"id": c.started.run.incident_id},
    )


async def authorization_counts(c):
    async with c.db() as session:
        return tuple(
            [
                await session.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in (
                    "action_plans",
                    "approval_requests",
                    "operation_executions",
                    "candidate_action_plans",
                    "candidate_approval_requests",
                    "condition_approval_requests",
                    "candidate_release_execution_requests",
                )
            ]
        )


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_candidate_consumes_exact_new_basis_and_recovers_without_reset(
    wait_context, monkeypatch
):
    c = wait_context
    execution = routing.review.synthesis.execution
    old_observation = execution.observation

    def observation(assignment, window, **kwargs):
        item = old_observation(assignment, window, **kwargs)
        return item.model_copy(
            update={
                "attributes": item.attributes
                | {
                    "change_type": "deployment",
                    "completed": True,
                    "from_version": "new-v1",
                    "to_version": "new-v2",
                }
            }
        )

    monkeypatch.setattr(execution, "observation", observation)
    await routing.setup(c)
    c.reflow_candidate_store = ReflowCandidateJobStore(c.db, clock=c.clock)
    c.reflow_candidate_ledger = ReflowCandidateLedger(c.db, clock=c.clock)
    c.app.state.reflow_candidate_queries = ReflowCandidateQueries(c.db, clock=c.clock)
    c.reflow_candidate_base = f"/api/v1/investigations/{c.started.run.id}/reinvestigation-candidate"
    errors = []
    old_fail = c.reflow_candidate_store.fail

    async def record_failure(*args, **kwargs):
        errors.append(str(sys.exception()))
        return await old_fail(*args, **kwargs)

    c.reflow_candidate_store.fail = record_failure
    assert not await c.reflow_candidate_store.prepare_next()
    assert (await get(c, {})).status_code == 401
    assert (await get(c, auth_header("alert_source"))).status_code == 403
    assert (await get(c)).json()["status"] == "pending"
    assert await c.routing_store.record_next()
    old_route = (await routing.get(c)).json()["record"]
    old_review = (await routing.review.get(c)).json()
    old_synthesis = (await routing.review.synthesis.get(c)).json()
    root = await execution.dispatch.coordination.root_facts(c)
    old_authorizations = await authorization_counts(c)
    before = await execution.dispatch.coordination.calls(c)
    prepared = await asyncio.gather(
        c.reflow_candidate_store.prepare_next(),
        c.reflow_candidate_store.prepare_next(),
    )
    assert sorted(prepared) == [False, True]
    claims = await asyncio.gather(
        *(
            c.reflow_candidate_store.claim_next(
                worker_id=f"claim-{i}", lease_duration=timedelta(seconds=20)
            )
            for i in range(2)
        )
    )
    assert sum(claim is not None for claim in claims) == 1
    claim = next(claim for claim in claims if claim is not None)
    _, packet = await c.reflow_candidate_store.get_packet(claim)
    grant = await c.reflow_candidate_ledger.reserve(claim)
    late_model = Model()
    late = await late_model.invoke(packet.model_input)
    wrote_settlement = False
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_call_reservations SET status='succeeded',"
                    "finished_at=clock_timestamp(),call_result=CAST(:r AS jsonb),"
                    "model_usage=CAST(:u AS jsonb) WHERE id=:id"
                ),
                {
                    "id": grant.record.id,
                    "r": late.model_dump_json(),
                    "u": late_model.usage.model_dump_json(),
                },
            )
            wrote_settlement = True
            while await session.scalar(
                text("SELECT clock_timestamp()<:deadline"), {"deadline": claim.lease_expires_at}
            ):
                await asyncio.sleep(0.5)
    assert wrote_settlement, "调用更新应先成功，拒绝必须发生在迟到COMMIT"
    # PG先到期时，先等应用时钟也满足过期前提，再断言应用层错误类型。
    while c.clock() < claim.lease_expires_at:
        await asyncio.sleep(0.05)
    with pytest.raises(ReflowCandidateError, match="租约"):
        await c.reflow_candidate_ledger.settle(
            claim, grant.record.id, status="succeeded", result=late, usage=late_model.usage
        )

    # 第二次尝试调用成功后模拟发布中断，第三次必须复用成功调用。
    complete = c.reflow_candidate_store.complete

    async def crash(*args):
        raise RuntimeError("模拟138成功调用后发布前崩溃")

    c.reflow_candidate_store.complete = crash

    async def inflight():
        assert (await routing.get(c)).status_code == 200
        assert (await routing.review.get(c)).json() == old_review
        calls = await execution.dispatch.coordination.calls(c)
        call_id = calls[-1]["id"]
        for suffix in ("model-input", "context-manifest", "interaction"):
            path = f"/api/v1/investigations/{c.started.run.id}/calls/{call_id}/{suffix}"
            response = await c.client.get(path, headers=auth_header("viewer"))
            assert response.status_code == 200, response.text
            assert (await c.client.get(path)).status_code == 401
        # 父尝试的失联收尾不能越权结算仍由独立阶段持有的预留。
        async with c.db() as session, session.begin():
            handoff = await session.scalar(
                select(InvestigationTerminalHandoffRow).where(
                    InvestigationTerminalHandoffRow.investigation_run_id == c.started.run.id
                )
            )
            assert handoff is not None
            await validated_handoff(session, handoff)
            await close_open_reservations(session, c.started.run.id, c.clock())
            assert (
                await session.scalar(
                    text("SELECT status FROM investigation_call_reservations WHERE id=:id"),
                    {"id": call_id},
                )
                == "reserved"
            )

    model = Model(inflight)
    assert await worker(c, model).run_once()
    queued = await get(c)
    assert queued.status_code == 200 and queued.json()["status"] == "queued", (queued.text, errors)
    assert len(model.calls) == 1
    async with c.db() as session, session.begin():
        rollback_alert = await session.begin_nested()
        await temporary_alert(session, c)
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "SELECT assert_reflow_candidate_source(j,true) "
                        "FROM investigation_reflow_candidate_jobs j"
                    )
                )
        await rollback_alert.rollback()
    c.reflow_candidate_store.complete = complete
    await asyncio.sleep(2)
    assert await worker(c, model).run_once()
    response = await get(c)
    assert response.status_code == 200 and response.json()["status"] == "succeeded", (
        response.text,
        errors,
    )
    body = response.json()
    evaluation = body["result"]["evaluation"]
    assert evaluation["disposition"] == "planning_review"
    assert evaluation["execution_eligible"] is False
    assert evaluation["step"]["parameters"]["target_version"] == "new-v1"
    assert len(model.calls) == 1 and len(body["attempts"]) == 3
    assert response.headers["cache-control"] == "no-store"
    assert not await worker(c, model).run_once()
    source = json.loads(model.calls[0].messages[1].content)["source"]
    assert source["routing"] == old_route
    assert source["review_result"] == old_review["result"]
    assert source["review_source"]["synthesis_result"] == old_synthesis["result"]
    assert body["result"]["routing_sha256"] == raw_content_hash(old_route)
    assert root == await execution.dispatch.coordination.root_facts(c)
    assert old_authorizations == await authorization_counts(c)
    assert old_review == (await routing.review.get(c)).json()
    assert old_synthesis == (await routing.review.synthesis.get(c)).json()
    assert old_route == (await routing.get(c)).json()["record"]
    calls = await execution.dispatch.coordination.calls(c)
    assert calls[:-2] == before
    assert [v["status"] for v in calls[-2:]] == ["unknown", "succeeded"]
    assert calls[-2]["model_allocation"] == calls[-1]["model_allocation"]
    assert all(v["reflow_candidate_attempt_id"] for v in calls[-2:])
    assert all(v["candidate_attempt_id"] is None for v in calls[-2:])
    async with c.db() as session:
        assert await session.scalar(
            text(
                "SELECT reflow_candidate_packet(source)=packet "
                "FROM investigation_reflow_candidate_jobs"
            )
        )
        snapshot = await session.scalar(
            text("SELECT reflow_candidate_budget_snapshot(:id,clock_timestamp())"),
            {"id": c.started.run.id},
        )
    assert snapshot == (await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)).model_dump(
        mode="json"
    )
    assert snapshot["model_usage"]["unknown_model_calls"] > 0

    for sql in (
        "UPDATE investigation_reflow_candidate_jobs SET source_sha256=repeat('0',64)",
        "UPDATE investigation_reflow_candidate_results SET content='{}'::jsonb",
        "DELETE FROM investigation_reflow_candidate_results",
        "DELETE FROM investigation_reflow_candidate_attempts",
    ):
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(text(sql))
    forged = copy.deepcopy(source)
    forged["routing"]["review_result_sha256"] = "0" * 64
    forged["routing_sha256"] = raw_content_hash(forged["routing"])
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await session.execute(text("SET LOCAL session_replication_role='replica'"))
            await session.execute(
                text(
                    "UPDATE investigation_reflow_candidate_jobs SET source=CAST(:s AS jsonb),"
                    "source_sha256=reflow_hash(CAST(:s AS jsonb)),"
                    "packet=reflow_candidate_packet(CAST(:s AS jsonb)),"
                    "packet_sha256=reflow_hash(reflow_candidate_packet(CAST(:s AS jsonb)))"
                ),
                {"s": json.dumps(forged)},
            )
            await session.execute(text("SET LOCAL session_replication_role='origin'"))
            await session.execute(
                text(
                    "SELECT assert_reflow_candidate_source(j,false) "
                    "FROM investigation_reflow_candidate_jobs j"
                )
            )
    with pytest.raises(DBAPIError):
        await asyncio.to_thread(command.downgrade, config(), "0092_reflow_routing")
    # 新告警只改变当前资格，已记录结果和137历史均保留。
    await execution.dispatch.coordination.admission.attach_old_alert(c)
    historical = await get(c)
    assert historical.status_code == 200 and historical.json() == body
    stale_route = await routing.get(c)
    assert stale_route.status_code == 200 and stale_route.json()["availability"] == "stale"
    assert stale_route.json()["record"] == old_route
