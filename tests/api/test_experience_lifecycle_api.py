"""真实业务链验证可信停用、跨事故范围检索和 PostgreSQL 锁等待。"""

import asyncio
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import select, text

import opsswarm.experience.lifecycle_store as lifecycle_module
import opsswarm.experience.search_store as search_module
from opsswarm.experience.contracts import ExperienceError
from opsswarm.experience.lifecycle_contracts import DisableCommand
from opsswarm.experience.lifecycle_tables import (
    ExperienceDisablementRow,
    ExperienceLifecycleCommandRow,
)
from opsswarm.experience.worker import ExperienceWorker
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, OperatorRole
from opsswarm.identity.settings import OperatorAuthSettings
from opsswarm.investigation.scenarios import DATABASE_POOL_SCENARIO_V2, RELEASE_500_SCENARIO
from tests.api.test_control_plane_api import BASE_TIME, alert_payload
from tests.api.test_experience_api import experience_context as experience_fixture
from tests.api.test_experience_review_api import command_for, count
from tests.experience_helpers import completed_source_report
from tests.identity_helpers import auth_header

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
experience_context = experience_fixture


async def history(context, factory, *, number=0, scenario=RELEASE_500_SCENARIO, decision="confirm"):
    app, client, clock = context
    clock.value = BASE_TIME
    report = await completed_source_report(
        client,
        factory,
        clock,
        source_key=f"lifecycle-{number}",
        scenario=scenario,
    )
    response = await client.post(
        f"/api/v1/incident-reports/{report['id']}/experience-generation-jobs",
        json={"expected_report_content_hash": report["content_hash"]},
        headers=auth_header("engineer"),
    )
    assert response.status_code == 201, response.text
    job = response.json()["job"]
    store = app.state.experience_store
    assert await ExperienceWorker(store, worker_id="lifecycle-history-worker").run_once()
    completed = await store.get(UUID(job["id"]))
    candidate = (await store.get_candidate(completed.candidate_id)).model_dump(mode="json")
    clock.advance(100 + number)
    if decision is None:
        return None, job
    reviewed = await client.post(
        f"/api/v1/experience-candidates/{candidate['id']}/reviews",
        json=command_for(candidate, decision=decision),
        headers=auth_header("engineer"),
    )
    assert reviewed.status_code == 201, reviewed.text
    return reviewed.json()["entry"], job


async def target(context, *, mode="frozen", service="order-service"):
    _, client, clock = context
    clock.value = BASE_TIME
    clock.advance(1000)
    payload = {
        **alert_payload(f"target-{uuid4()}"),
        "service_name": service,
        "fingerprint": f"target:{uuid4()}",
    }
    response = await client.post(
        "/api/v1/alerts", json=payload, headers=auth_header("alert_source")
    )
    incident = response.json()["incident"]
    started = await client.post(
        f"/api/v1/incidents/{incident['id']}/actions/start-investigation",
        json={
            "expected_version": 1,
            "reason": "检索已确认历史参考",
            "mode": "single_agent",
            "data_source_mode": mode,
        },
        headers=auth_header("engineer"),
    )
    assert started.status_code == 200, started.text
    return started.json()["investigation"]["id"]


def disable_body(entry, **overrides):
    return {
        "idempotency_key": "disable-command-001",
        "expected_entry_version": entry["version"],
        "expected_entry_content_hash": entry["content_hash"],
        "expected_state_version": 1,
        "reason": "历史前置条件不再适用，停止新检索引用",
        **overrides,
    }


async def disable(client, entry, body=None, *, role="engineer"):
    return await client.post(
        f"/api/v1/experience-entries/{entry['id']}/actions/disable",
        json=body if body is not None else disable_body(entry),
        headers=auth_header(role),
    )


async def search(client, run_id, **query):
    response = await client.get(
        f"/api/v1/investigations/{run_id}/experience-entries",
        params=query,
        headers=auth_header("viewer"),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def audit(factory):
    async with factory() as session:
        return list(await session.scalars(select(ExperienceLifecycleCommandRow)))


async def test_scope_ranking_limit_disable_and_historical_detail(
    experience_context, database_session_factory
):
    histories = [
        await history(
            experience_context,
            database_session_factory,
            number=i,
            scenario=DATABASE_POOL_SCENARIO_V2 if i == 0 else RELEASE_500_SCENARIO,
        )
        for i in range(4)
    ]
    _, client, _ = experience_context
    run_id = await target(experience_context)
    entries = [entry for entry, _ in histories]
    default = await search(client, run_id)
    assert [item["entry_id"] for item in default["items"]] == [
        entry["id"] for entry in entries[:0:-1]
    ]
    assert default["scope"]["data_source_mode"] == "frozen"
    preferred = await search(client, run_id, preferred_fault_tag="database_pool_misconfiguration")
    assert [item["entry_id"] for item in preferred["items"]] == [
        entries[i]["id"] for i in (0, 3, 2)
    ]
    assert all("尚未经过经验审核" not in item["historical_summary"] for item in preferred["items"])
    assert all("可信工程师" in item["confirmation_notice"] for item in preferred["items"])
    before = await client.get(
        f"/api/v1/experience-entries/{entries[3]['id']}/state", headers=auth_header("viewer")
    )
    assert before.json()["status"] == "active" and before.json()["state_version"] == 1
    first = await disable(client, entries[3])
    repeated = await disable(client, entries[3])
    assert first.status_code == 201, first.text
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == {**first.json(), "applied": False}
    assert first.json()["state"]["state_version"] == 2
    assert first.json()["state"]["disablement"]["disabled_by_id"] == "trusted-engineer"
    assert (
        first.json()["state"]["disablement"]["authorization_id"]
        == first.headers["x-authorization-id"]
    )
    after = await search(client, run_id)
    assert [item["entry_id"] for item in after["items"]] == [entries[i]["id"] for i in (2, 1, 0)]
    old = await client.get(
        f"/api/v1/experience-entries/{entries[3]['id']}", headers=auth_header("viewer")
    )
    assert old.json() == entries[3]
    assert {a.outcome for a in await audit(database_session_factory)} == {"applied", "replayed"}


@pytest.mark.parametrize(
    ("mode", "service"), [("live", "order-service"), ("frozen", "payment-service")]
)
async def test_system_scope_never_mixes_other_sources_or_services(
    experience_context,
    database_session_factory,
    mode,
    service,
):
    await history(experience_context, database_session_factory)
    _, client, _ = experience_context
    run_id = await target(experience_context, mode=mode, service=service)
    result = await search(client, run_id)
    assert result["items"] == []
    assert (
        result["scope"]["data_source_mode"] == mode and result["scope"]["service_name"] == service
    )


async def test_own_incident_pending_and_rejected_candidates_do_not_enter_results(
    experience_context,
    database_session_factory,
):
    _, first_job = await history(experience_context, database_session_factory)
    _, client, _ = experience_context
    assert (await search(client, first_job["source"]["investigation"]["id"]))["items"] == []
    # 无确认版本的候选即使有完整来源，也不能产生可用状态或检索条目。
    await history(experience_context, database_session_factory, number=1, decision=None)
    await history(experience_context, database_session_factory, number=2, decision="reject")
    run_id = await target(experience_context)
    assert len((await search(client, run_id))["items"]) == 1


@pytest.mark.parametrize("role", [None, "viewer", "approver", "executor", "alert_source"])
async def test_only_trusted_engineer_can_disable_before_lookup(experience_context, role):
    _, client, _ = experience_context
    response = await client.post(
        f"/api/v1/experience-entries/{uuid4()}/actions/disable",
        json={},
        headers=auth_header(role) if role else {},
    )
    assert response.status_code == (403 if role else 401)


async def test_legacy_and_scope_override_are_rejected(experience_context):
    app, client, _ = experience_context
    run_id = await target(experience_context)
    for query in (
        {"service_name": "other"},
        {"data_source_mode": "live"},
        {"limit": "100"},
        {"preferred_fault_tag": "invented-root-cause"},
    ):
        response = await client.get(
            f"/api/v1/investigations/{run_id}/experience-entries",
            params=query,
            headers=auth_header("viewer"),
        )
        assert response.status_code == 422, response.text
    assert (
        await client.get(
            f"/api/v1/investigations/{run_id}/experience-entries",
            headers=auth_header("alert_source"),
        )
    ).status_code == 403
    app.state.operator_auth_settings = OperatorAuthSettings(mode="legacy_local")
    for path in (
        f"/api/v1/experience-entries/{uuid4()}/state",
        f"/api/v1/investigations/{run_id}/experience-entries",
    ):
        assert (await client.get(path)).status_code == 401
    assert (
        await client.post(f"/api/v1/experience-entries/{uuid4()}/actions/disable", json={})
    ).status_code == 401


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"expected_entry_version": 2}, "experience_entry_version_conflict"),
        ({"expected_entry_content_hash": "0" * 64}, "experience_entry_hash_conflict"),
        ({"expected_state_version": 2}, "experience_state_version_conflict"),
    ],
)
async def test_version_conflicts_are_audited_without_disabling(
    experience_context,
    database_session_factory,
    override,
    code,
):
    entry, _ = await history(experience_context, database_session_factory)
    _, client, _ = experience_context
    response = await disable(client, entry, disable_body(entry, **override))
    assert response.status_code == 409 and response.json()["error_code"] == code
    assert await count(database_session_factory, ExperienceDisablementRow) == 0
    result = await audit(database_session_factory)
    assert len(result) == 1 and result[0].error_code == code and result[0].http_status == 409


async def test_concurrent_disable_and_terminal_conflicts_are_persistent(
    experience_context,
    database_session_factory,
):
    entry, _ = await history(experience_context, database_session_factory)
    _, client, _ = experience_context
    results = await asyncio.gather(*(disable(client, entry) for _ in range(3)))
    assert sorted(r.status_code for r in results) == [200, 200, 201]
    changed = await disable(client, entry, disable_body(entry, reason="不同理由"))
    other = await disable(client, entry, disable_body(entry, idempotency_key="different-command"))
    assert changed.json()["error_code"] == "experience_disable_idempotency_conflict"
    assert other.json()["error_code"] == "experience_entry_already_disabled"
    assert changed.status_code == other.status_code == 409
    assert await count(database_session_factory, ExperienceDisablementRow) == 1
    assert len(await audit(database_session_factory)) == 5


async def test_missing_entities_and_forged_command(experience_context, database_session_factory):
    _, client, _ = experience_context
    missing = {"id": str(uuid4()), "version": 1, "content_hash": "a" * 64}
    response = await disable(client, missing)
    assert response.status_code == 404
    result = await audit(database_session_factory)
    assert len(result) == 1 and result[0].entry_id == UUID(missing["id"])
    forged = await disable(client, missing, disable_body(missing, disabled_by_id="forged"))
    assert forged.status_code == 422 and len(await audit(database_session_factory)) == 1
    for path in (
        f"/api/v1/experience-entries/{uuid4()}/state",
        f"/api/v1/investigations/{uuid4()}/experience-entries",
    ):
        assert (await client.get(path, headers=auth_header("viewer"))).status_code == 404


@pytest.mark.parametrize("table", ["experience_disablements", "experience_lifecycle_commands"])
async def test_database_failure_rolls_back_fact_and_result(
    experience_context, database_session_factory, table
):
    entry, _ = await history(experience_context, database_session_factory)
    app, client, _ = experience_context
    async with database_session_factory() as session, session.begin():
        await session.execute(
            text(f"""
            CREATE FUNCTION test_disable_failure() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION '测试停用事务失败'; END; $$ LANGUAGE plpgsql;
            CREATE TRIGGER test_disable_failure_insert AFTER INSERT ON {table}
                FOR EACH ROW EXECUTE FUNCTION test_disable_failure();
        """)
        )
    try:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as failing:
            response = await disable(failing, entry)
        assert response.status_code == 500 and "测试停用事务失败" not in response.text
        assert await count(database_session_factory, ExperienceDisablementRow) == 0
        assert await count(database_session_factory, ExperienceLifecycleCommandRow) == 0
    finally:
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    f"DROP TRIGGER test_disable_failure_insert ON {table}; "
                    "DROP FUNCTION test_disable_failure();"
                )
            )
    assert (await disable(client, entry)).status_code == 201


async def test_post_write_validation_refusal_preserves_only_error_audit(
    experience_context,
    database_session_factory,
    monkeypatch,
):
    entry, _ = await history(experience_context, database_session_factory)
    _, client, _ = experience_context

    def refuse(*args):
        raise ExperienceError("experience_disablement_integrity_conflict", "测试停用校验拒绝")

    monkeypatch.setattr(lifecycle_module, "validated_disablement", refuse)
    response = await disable(client, entry)
    assert response.status_code == 409
    assert await count(database_session_factory, ExperienceDisablementRow) == 0
    assert (await audit(database_session_factory))[0].outcome == "error"


async def wait_for_lock(factory):
    async with asyncio.timeout(8):
        while True:
            async with factory() as session:
                waiting = await session.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                        "AND query LIKE '%experience_entries%' AND pid <> pg_backend_pid()"
                    )
                )
            if waiting:
                return
            await asyncio.sleep(0.02)


async def test_search_rechecks_disablement_after_waiting_for_writer_lock(
    experience_context,
    database_session_factory,
    monkeypatch,
):
    entry, _ = await history(experience_context, database_session_factory)
    _, client, _ = experience_context
    run_id = await target(experience_context)
    locked, release = asyncio.Event(), asyncio.Event()
    original = lifecycle_module._locked_entry

    async def paused(session, entry_id, *, read):
        result = await original(session, entry_id, read=read)
        if not read:
            locked.set()
            await release.wait()
        return result

    monkeypatch.setattr(lifecycle_module, "_locked_entry", paused)
    writer = asyncio.create_task(disable(client, entry))
    await asyncio.wait_for(locked.wait(), timeout=8)
    reader = asyncio.create_task(search(client, run_id))
    try:
        await wait_for_lock(database_session_factory)
    finally:
        release.set()
    assert (await writer).status_code == 201
    assert (await reader)["items"] == []


async def test_search_shared_lock_orders_disable_after_read_snapshot(
    experience_context,
    database_session_factory,
    monkeypatch,
):
    entry, _ = await history(experience_context, database_session_factory)
    _, client, _ = experience_context
    run_id = await target(experience_context)
    locked, release = asyncio.Event(), asyncio.Event()
    original = search_module.load_confirmed_entry_in_session

    async def paused(session, entry_id):
        result = await original(session, entry_id)
        locked.set()
        await release.wait()
        return result

    monkeypatch.setattr(search_module, "load_confirmed_entry_in_session", paused)
    reader = asyncio.create_task(search(client, run_id))
    await asyncio.wait_for(locked.wait(), timeout=8)
    writer = asyncio.create_task(disable(client, entry))
    try:
        await wait_for_lock(database_session_factory)
    finally:
        release.set()
    assert [i["entry_id"] for i in (await reader)["items"]] == [entry["id"]]
    assert (await writer).status_code == 201
    assert (await search(client, run_id))["items"] == []


async def test_report_association_drift_never_overwrites_frozen_source_identity(
    experience_context,
    database_session_factory,
):
    entry, job = await history(experience_context, database_session_factory)
    _, client, _ = experience_context
    run_id = await target(experience_context)
    scope = (await search(client, run_id))["scope"]
    async with database_session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE incident_reports SET incident_id = :incident_id WHERE id = :report_id"),
            {"incident_id": UUID(scope["incident_id"]), "report_id": UUID(job["report_id"])},
        )
    assert (await search(client, job["source"]["investigation"]["id"]))["items"] == []
    result = await search(client, run_id)
    assert [item["entry_id"] for item in result["items"]] == [entry["id"]]
    assert result["items"][0]["source_incident_id"] == job["source"]["incident_id"]
    assert (await disable(client, entry)).status_code == 201
    assert (await search(client, run_id))["items"] == []


async def test_service_expiry_and_wrong_authorization_fail_before_idempotent_return(
    experience_context,
    database_session_factory,
):
    entry, job = await history(experience_context, database_session_factory)
    app, client, clock = experience_context
    principal = OperatorPrincipal("trusted-engineer", "engineer", OperatorRole.ENGINEER, clock())
    store = app.state.experience_lifecycle_store
    command = DisableCommand.model_validate(disable_body(entry))
    with pytest.raises(OperatorAccessError):
        await store.disable(
            UUID(entry["id"]), command, principal=principal, authorization_id=uuid4()
        )
    principal = OperatorPrincipal(
        "trusted-engineer", "engineer", OperatorRole.ENGINEER, BASE_TIME.replace(day=2)
    )
    with pytest.raises(OperatorAccessError) as error:
        await store.disable(
            UUID(entry["id"]),
            command,
            principal=principal,
            authorization_id=UUID(job["authorization_id"]),
        )
    assert error.value.code == "operator_permission_denied"
    assert (await disable(client, entry)).status_code == 201
    clock.advance(24 * 3600)
    assert (await disable(client, entry)).status_code == 401
    assert len(await audit(database_session_factory)) == 1
