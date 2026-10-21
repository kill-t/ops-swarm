"""真实 PostgreSQL 业务链验证替代、历史追溯和并发终态。"""

import asyncio
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import select, text

import opsswarm.experience.replacement_store as replacement_module
import opsswarm.experience.search_store as search_module
from opsswarm.experience.contracts import ExperienceError
from opsswarm.experience.replacement_contracts import ReplaceCommand
from opsswarm.experience.replacement_tables import (
    ExperienceReplacementCommandRow,
    ExperienceReplacementRow,
)
from opsswarm.experience.review_tables import ExperienceEntryRow
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, OperatorRole
from opsswarm.identity.settings import OperatorAuthSettings
from opsswarm.investigation.scenarios import DATABASE_POOL_SCENARIO_V2
from tests.api.test_control_plane_api import BASE_TIME
from tests.api.test_experience_lifecycle_api import (
    disable,
    history,
    search,
    target,
    wait_for_lock,
)
from tests.api.test_experience_lifecycle_api import (
    experience_context as experience_fixture,
)
from tests.api.test_experience_review_api import count
from tests.identity_helpers import auth_header

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
experience_context = experience_fixture


async def pair(context, factory):
    old, _ = await history(context, factory, number=0)
    new, job = await history(context, factory, number=1)
    return old, new, job


def replace_body(entry, new, **overrides):
    return {
        "idempotency_key": "replace-command-001",
        "expected_entry_version": entry["version"],
        "expected_entry_content_hash": entry["content_hash"],
        "expected_state_version": 1,
        "target_entry_id": new["id"],
        "expected_target_version": new["version"],
        "expected_target_content_hash": new["content_hash"],
        "expected_target_state_version": 1,
        "reason": "以较新且人工确认的历史材料替代旧参考",
        **overrides,
    }


async def replace(client, entry, new, body=None, *, role="engineer"):
    return await client.post(
        f"/api/v1/experience-entries/{entry['id']}/actions/replace",
        json=body if body is not None else replace_body(entry, new),
        headers=auth_header(role),
    )


async def audit(factory):
    async with factory() as session:
        return list(await session.scalars(select(ExperienceReplacementCommandRow)))


async def test_chain_keeps_exact_history_after_successor_is_replaced_and_disabled(
    experience_context,
    database_session_factory,
):
    old, middle, _ = await pair(experience_context, database_session_factory)
    new, _ = await history(experience_context, database_session_factory, number=2)
    _, client, _ = experience_context
    run_id = await target(experience_context)
    first = await replace(client, old, middle)
    assert first.status_code == 201, first.text
    fact = first.json()["replacement"]
    assert fact["authorization_id"] == first.headers["x-authorization-id"]
    assert fact["entry_content_hash"] == old["content_hash"]
    assert fact["target_content_hash"] == middle["content_hash"]
    assert [i["entry_id"] for i in (await search(client, run_id))["items"]] == [
        new["id"],
        middle["id"],
    ]
    assert (await replace(client, middle, new)).status_code == 201
    assert [i["entry_id"] for i in (await search(client, run_id))["items"]] == [new["id"]]
    assert (await disable(client, new)).status_code == 201
    assert (await search(client, run_id))["items"] == []
    replay = await replace(client, old, middle)
    assert replay.status_code == 200 and replay.json() == {**first.json(), "applied": False}
    state = await client.get(
        f"/api/v1/experience-entries/{old['id']}/state", headers=auth_header("viewer")
    )
    assert state.json()["status"] == "replaced" and state.json()["state_version"] == 2
    assert state.json()["disablement"] is None and state.json()["replacement"] == fact
    old_detail = await client.get(
        f"/api/v1/experience-entries/{old['id']}", headers=auth_header("viewer")
    )
    assert old_detail.json() == old
    refused = await disable(client, old)
    assert refused.status_code == 409
    assert refused.json()["error_code"] == "experience_entry_already_replaced"
    assert {r.outcome for r in await audit(database_session_factory)} == {"applied", "replayed"}


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"expected_entry_version": 2}, "experience_entry_version_conflict"),
        ({"expected_entry_content_hash": "0" * 64}, "experience_entry_hash_conflict"),
        ({"expected_state_version": 2}, "experience_state_version_conflict"),
        ({"expected_target_version": 2}, "experience_target_version_conflict"),
        ({"expected_target_content_hash": "0" * 64}, "experience_target_hash_conflict"),
        ({"expected_target_state_version": 2}, "experience_target_state_conflict"),
    ],
)
async def test_both_sides_require_exact_versions_and_hashes(
    experience_context,
    database_session_factory,
    override,
    code,
):
    old, new, _ = await pair(experience_context, database_session_factory)
    _, client, _ = experience_context
    result = await replace(client, old, new, replace_body(old, new, **override))
    assert result.status_code == 409 and result.json()["error_code"] == code
    assert await count(database_session_factory, ExperienceReplacementRow) == 0
    rows = await audit(database_session_factory)
    assert len(rows) == 1 and rows[0].error_code == code
    # 拒绝不占用业务键，修正精确版本后原键仍能成功。
    assert (await replace(client, old, new)).status_code == 201


@pytest.mark.parametrize("role", [None, "viewer", "approver", "executor", "alert_source"])
async def test_permission_is_checked_before_body_and_lookup(experience_context, role):
    _, client, _ = experience_context
    response = await client.post(
        f"/api/v1/experience-entries/{uuid4()}/actions/replace",
        json={},
        headers=auth_header(role) if role else {},
    )
    assert response.status_code == (401 if role is None else 403)


async def test_legacy_and_forged_actor_are_rejected(experience_context, database_session_factory):
    old, new, _ = await pair(experience_context, database_session_factory)
    app, client, _ = experience_context
    forged = await replace(client, old, new, replace_body(old, new, replaced_by_id="forged"))
    assert forged.status_code == 422 and len(await audit(database_session_factory)) == 0
    app.state.operator_auth_settings = OperatorAuthSettings(mode="legacy_local")
    assert (await replace(client, old, new)).status_code == 401


@pytest.mark.parametrize("missing_side", ["entry", "target"])
async def test_unknown_side_is_audited(experience_context, database_session_factory, missing_side):
    old, new, _ = await pair(experience_context, database_session_factory)
    if missing_side == "entry":
        old = {**old, "id": str(uuid4())}
    else:
        new = {**new, "id": str(uuid4())}
    _, client, _ = experience_context
    assert (await replace(client, old, new)).status_code == 404
    rows = await audit(database_session_factory)
    assert len(rows) == 1 and rows[0].entry_id == UUID(old["id"])
    assert rows[0].target_entry_id == UUID(new["id"]) and rows[0].http_status == 404


async def test_identical_concurrency_and_changed_command_preserve_first_fact(
    experience_context,
    database_session_factory,
):
    old, new, _ = await pair(experience_context, database_session_factory)
    _, client, _ = experience_context
    results = await asyncio.gather(*(replace(client, old, new) for _ in range(3)))
    assert sorted(r.status_code for r in results) == [200, 200, 201]
    for changes in ({"reason": "改原因"}, {"target_entry_id": str(uuid4())}):
        changed = await replace(client, old, new, replace_body(old, new, **changes))
        assert changed.status_code == 409
        assert changed.json()["error_code"] == "experience_replace_idempotency_conflict"
    other = await replace(client, old, new, replace_body(old, new, idempotency_key="other-command"))
    assert other.status_code == 409
    assert await count(database_session_factory, ExperienceReplacementRow) == 1
    assert len(await audit(database_session_factory)) == 6


@pytest.mark.parametrize("shared_side", ["entry", "target"])
async def test_competing_replacements_have_one_winner(
    experience_context,
    database_session_factory,
    shared_side,
):
    first, middle, _ = await pair(experience_context, database_session_factory)
    last, _ = await history(experience_context, database_session_factory, number=2)
    _, client, _ = experience_context
    pairs = (
        [(first, middle), (first, last)]
        if shared_side == "entry"
        else [(first, last), (middle, last)]
    )
    results = await asyncio.gather(*(replace(client, old, new) for old, new in pairs))
    assert sorted(r.status_code for r in results) == [201, 409]
    assert await count(database_session_factory, ExperienceReplacementRow) == 1
    assert {r.outcome for r in await audit(database_session_factory)} == {"applied", "error"}


@pytest.mark.parametrize("disabled_side", ["entry", "target"])
async def test_disable_and_replace_serialize_without_dual_terminal(
    experience_context,
    database_session_factory,
    disabled_side,
):
    old, new, _ = await pair(experience_context, database_session_factory)
    _, client, _ = experience_context
    replaced, disabled = await asyncio.gather(
        replace(client, old, new), disable(client, old if disabled_side == "entry" else new)
    )
    assert replaced.status_code in (201, 409) and disabled.status_code in (201, 409)
    if disabled_side == "entry":
        assert sorted((replaced.status_code, disabled.status_code)) == [201, 409]
    else:
        assert disabled.status_code == 201
    async with database_session_factory() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM experience_disablements d "
                    "JOIN experience_replacements r ON r.entry_id = d.entry_id"
                )
            )
            == 0
        )


@pytest.mark.parametrize("table", ["experience_replacements", "experience_replacement_commands"])
async def test_sql_failure_rolls_back_fact_and_business_result(
    experience_context,
    database_session_factory,
    table,
):
    old, new, _ = await pair(experience_context, database_session_factory)
    app, client, _ = experience_context
    async with database_session_factory() as session, session.begin():
        await session.execute(
            text(f"""
            CREATE FUNCTION test_replace_failure() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION '测试替代事务失败'; END; $$ LANGUAGE plpgsql;
            CREATE TRIGGER test_replace_failure_insert AFTER INSERT ON {table}
                FOR EACH ROW EXECUTE FUNCTION test_replace_failure();
        """)
        )
    try:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as failing:
            response = await replace(failing, old, new)
        assert response.status_code == 500 and "测试替代事务失败" not in response.text
        assert await count(database_session_factory, ExperienceReplacementRow) == 0
        assert len(await audit(database_session_factory)) == 0
    finally:
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    f"DROP TRIGGER test_replace_failure_insert ON {table}; "
                    "DROP FUNCTION test_replace_failure();"
                )
            )
    assert (await replace(client, old, new)).status_code == 201


async def test_post_write_business_refusal_preserves_only_error_result(
    experience_context,
    database_session_factory,
    monkeypatch,
):
    old, new, _ = await pair(experience_context, database_session_factory)
    _, client, _ = experience_context

    def refuse(*args):
        raise ExperienceError("experience_replacement_integrity_conflict", "测试写后拒绝")

    monkeypatch.setattr(replacement_module, "validated_replacement", refuse)
    result = await replace(client, old, new)
    assert result.status_code == 409
    assert await count(database_session_factory, ExperienceReplacementRow) == 0
    assert (await audit(database_session_factory))[0].outcome == "error"


async def test_expiry_and_wrong_admission_are_checked_before_replay(
    experience_context,
    database_session_factory,
):
    old, new, job = await pair(experience_context, database_session_factory)
    app, client, clock = experience_context
    command = ReplaceCommand.model_validate(replace_body(old, new))
    store = app.state.experience_replacement_store
    expired = OperatorPrincipal("trusted-engineer", "engineer", OperatorRole.ENGINEER, clock())
    with pytest.raises(OperatorAccessError):
        await store.replace(UUID(old["id"]), command, principal=expired, authorization_id=uuid4())
    principal = OperatorPrincipal(
        "trusted-engineer", "engineer", OperatorRole.ENGINEER, BASE_TIME.replace(day=2)
    )
    with pytest.raises(OperatorAccessError) as error:
        await store.replace(
            UUID(old["id"]),
            command,
            principal=principal,
            authorization_id=UUID(job["authorization_id"]),
        )
    assert error.value.code == "operator_permission_denied"
    assert (await replace(client, old, new)).status_code == 201
    clock.advance(24 * 3600)
    assert (await replace(client, old, new)).status_code == 401
    assert len(await audit(database_session_factory)) == 1


async def test_search_rechecks_after_waiting_for_replacement(
    experience_context,
    database_session_factory,
    monkeypatch,
):
    old, new, _ = await pair(experience_context, database_session_factory)
    _, client, _ = experience_context
    run_id = await target(experience_context)
    locked, release = asyncio.Event(), asyncio.Event()
    original = replacement_module.lock_replacement_entries

    async def paused(session, entry_id, target_id):
        await original(session, entry_id, target_id)
        locked.set()
        await release.wait()

    monkeypatch.setattr(replacement_module, "lock_replacement_entries", paused)
    writer = asyncio.create_task(replace(client, old, new))
    await asyncio.wait_for(locked.wait(), timeout=8)
    reader = asyncio.create_task(search(client, run_id))
    try:
        await wait_for_lock(database_session_factory)
    finally:
        release.set()
    assert (await writer).status_code == 201
    result = await reader
    assert result["policy_version"] == "experience-search-v2"
    assert [i["entry_id"] for i in result["items"]] == [new["id"]]


async def test_search_shared_locks_order_replacement_after_read_snapshot(
    experience_context,
    database_session_factory,
    monkeypatch,
):
    old, new, _ = await pair(experience_context, database_session_factory)
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
    writer = asyncio.create_task(replace(client, old, new))
    try:
        await wait_for_lock(database_session_factory)
    finally:
        release.set()
    assert [i["entry_id"] for i in (await reader)["items"]] == [new["id"], old["id"]]
    assert (await writer).status_code == 201
    assert [i["entry_id"] for i in (await search(client, run_id))["items"]] == [new["id"]]


async def test_rank_opposite_to_uuid_lock_order_cannot_deadlock_with_replacement(
    experience_context,
    database_session_factory,
    monkeypatch,
):
    old, _ = await history(experience_context, database_session_factory, number=0)
    new, _ = await history(
        experience_context, database_session_factory, number=1, scenario=DATABASE_POOL_SCENARIO_V2
    )
    _, client, _ = experience_context
    run_id = await target(experience_context)
    ordered = sorted((UUID(old["id"]), UUID(new["id"])), key=lambda value: value.int)
    largest = max((old, new), key=lambda value: UUID(value["id"]).int)
    preference = largest["content"]["candidate_snapshot"]["fault_tags"][0]
    query = {"preferred_fault_tag": preference}
    ranked = await search(client, run_id, **query)
    assert [item["entry_id"] for item in ranked["items"]] == [str(i) for i in ordered[::-1]]
    first_locked, acquire_second = asyncio.Event(), asyncio.Event()

    async def paused_between_locks(session, entry_id, target_id):
        assert {entry_id, target_id} == set(ordered)
        await session.scalar(
            select(ExperienceEntryRow.id)
            .where(ExperienceEntryRow.id == ordered[0])
            .with_for_update()
        )
        first_locked.set()
        await acquire_second.wait()
        await session.scalar(
            select(ExperienceEntryRow.id)
            .where(ExperienceEntryRow.id == ordered[1])
            .with_for_update()
        )

    monkeypatch.setattr(replacement_module, "lock_replacement_entries", paused_between_locks)
    writer = asyncio.create_task(replace(client, old, new))
    await asyncio.wait_for(first_locked.wait(), timeout=8)
    reader = asyncio.create_task(search(client, run_id, **query))
    try:
        await wait_for_lock(database_session_factory)
    finally:
        acquire_second.set()
    assert (await asyncio.wait_for(writer, timeout=8)).status_code == 201
    assert [i["entry_id"] for i in (await asyncio.wait_for(reader, timeout=8))["items"]] == [
        new["id"]
    ]
