"""真实 PostgreSQL 验证可信人工审核、并发决定和不可变确认版本。"""

import asyncio
import hashlib
from datetime import timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from sqlalchemy import func, select, text

import opsswarm.experience.review_store as review_store_module
from opsswarm.experience.contracts import ExperienceError
from opsswarm.experience.review_contracts import ReviewCommand
from opsswarm.experience.review_store import ExperienceReviewStore
from opsswarm.experience.review_tables import (
    ExperienceEntryRow,
    ExperienceReviewCommandRow,
    ExperienceReviewRow,
)
from opsswarm.experience.worker import ExperienceWorker
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, OperatorRole
from opsswarm.identity.settings import OperatorAuthSettings, OperatorCredential
from tests.api.test_experience_api import create_job
from tests.api.test_experience_api import experience_context as experience_fixture
from tests.identity_helpers import auth_header, auth_settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
experience_context = experience_fixture


async def candidate_ready(context, factory):
    store, job, _ = await create_job(context, factory)
    assert await ExperienceWorker(store, worker_id="review-source-worker").run_once()
    completed = await store.get(UUID(job["id"]))
    candidate = await store.get_candidate(completed.candidate_id)
    return candidate.model_dump(mode="json"), job


def command_for(candidate, **overrides):
    return {
        "idempotency_key": "review-command-001",
        "expected_candidate_version": candidate["version"],
        "expected_candidate_content_hash": candidate["content_hash"],
        "expected_review_version": 0,
        "decision": "confirm",
        "reason": "核对来源及反证后确认，限定为本地历史参考",
        **overrides,
    }


async def submit(client, candidate, body=None, *, role="engineer"):
    return await client.post(
        f"/api/v1/experience-candidates/{candidate['id']}/reviews",
        json=body if body is not None else command_for(candidate),
        headers=auth_header(role),
    )


async def commands(factory):
    async with factory() as session:
        return list(
            await session.scalars(
                select(ExperienceReviewCommandRow).order_by(
                    ExperienceReviewCommandRow.created_at,
                    ExperienceReviewCommandRow.http_status.desc(),
                )
            )
        )


async def count(factory, model):
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize("decision", ["confirm", "reject"])
async def test_trusted_review_full_chain_preserves_candidate_and_audits_replay(
    experience_context,
    database_session_factory,
    decision,
):
    candidate, _ = await candidate_ready(experience_context, database_session_factory)
    app, client, _ = experience_context
    path = f"/api/v1/experience-candidates/{candidate['id']}/review"
    pending = await client.get(path, headers=auth_header("viewer"))
    assert pending.status_code == 200 and pending.json()["review_version"] == 0
    assert pending.json()["status"] == "pending_review" and pending.json()["review"] is None
    body = command_for(candidate, decision=decision)
    first = await submit(client, candidate, body)
    repeated = await submit(client, candidate, body)
    assert first.status_code == 201, first.text
    assert repeated.status_code == 200, repeated.text
    result = first.json()
    assert result["applied"] is True and repeated.json() == {**result, "applied": False}
    assert result["review"]["reviewed_by_id"] == "trusted-engineer"
    assert result["review"]["authorization_id"] == first.headers["x-authorization-id"]
    assert repeated.headers["x-authorization-id"] != first.headers["x-authorization-id"]
    state = (await client.get(path, headers=auth_header("viewer"))).json()
    assert state["status"] == ("confirmed" if decision == "confirm" else "rejected")
    assert state["review_version"] == 1 and state["review"] == result["review"]
    original = await client.get(
        f"/api/v1/experience-candidates/{candidate['id']}", headers=auth_header("viewer")
    )
    assert original.json() == candidate
    if decision == "confirm":
        entry = result["entry"]
        assert entry["status"] == "confirmed" and entry["version"] == 1
        assert entry["content"]["schema_version"] == "confirmed-experience-v1"
        assert entry["content"]["candidate_snapshot"] == candidate["content"]
        assert "可信工程师" in entry["content"]["confirmation_notice"]
        fetched = await client.get(
            f"/api/v1/experience-entries/{entry['id']}", headers=auth_header("viewer")
        )
        assert fetched.json() == entry and state["entry_id"] == entry["id"]
        restarted = ExperienceReviewStore(database_session_factory)
        assert (await restarted.get_entry(UUID(entry["id"]))).id == UUID(entry["id"])
    else:
        assert result["entry"] is None and state["entry_id"] is None
    assert await count(database_session_factory, ExperienceReviewRow) == 1
    assert await count(database_session_factory, ExperienceEntryRow) == (decision == "confirm")
    audit = await commands(database_session_factory)
    assert {row.outcome for row in audit} == {"applied", "replayed"}
    assert {row.review_id for row in audit} == {UUID(result["review"]["id"])}
    assert app.state.experience_review_store is not None


@pytest.mark.parametrize("role", [None, "viewer", "approver", "executor", "alert_source"])
async def test_only_trusted_engineer_can_review_before_candidate_lookup(experience_context, role):
    _, client, _ = experience_context
    response = await client.post(
        f"/api/v1/experience-candidates/{uuid4()}/reviews",
        json={},
        headers=auth_header(role) if role else {},
    )
    assert response.status_code == (403 if role else 401)


async def test_review_and_entry_routes_refuse_legacy_identity(experience_context):
    app, client, _ = experience_context
    app.state.operator_auth_settings = OperatorAuthSettings(mode="legacy_local")
    assert (
        await client.post(
            f"/api/v1/experience-candidates/{uuid4()}/reviews", json={"actor_id": "engineer"}
        )
    ).status_code == 401
    for path in (
        f"/api/v1/experience-candidates/{uuid4()}/review",
        f"/api/v1/experience-entries/{uuid4()}",
    ):
        assert (await client.get(path)).status_code == 401


@pytest.mark.parametrize(
    "overrides",
    [
        {"actor_id": "another-engineer"},
        {"content": {}},
        {"decision": "enable"},
        {"reason": " "},
        {"idempotency_key": "short"},
        {"idempotency_key": "非法字符-key"},
    ],
)
async def test_forged_or_invalid_review_body_never_writes_decision(
    experience_context,
    database_session_factory,
    overrides,
):
    _, client, _ = experience_context
    candidate = {"id": str(uuid4()), "version": 1, "content_hash": "a" * 64}
    response = await submit(client, candidate, command_for(candidate, **overrides))
    assert response.status_code == 422
    assert await count(database_session_factory, ExperienceReviewRow) == 0
    assert await count(database_session_factory, ExperienceReviewCommandRow) == 0


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"expected_candidate_version": 2}, "experience_candidate_version_conflict"),
        ({"expected_candidate_content_hash": "0" * 64}, "experience_candidate_hash_conflict"),
        ({"expected_review_version": 1}, "experience_review_version_conflict"),
    ],
)
async def test_optimistic_conflicts_persist_business_results_without_deciding(
    experience_context,
    database_session_factory,
    overrides,
    code,
):
    candidate, _ = await candidate_ready(experience_context, database_session_factory)
    _, client, _ = experience_context
    response = await submit(client, candidate, command_for(candidate, **overrides))
    assert response.status_code == 409 and response.json()["error_code"] == code
    assert await count(database_session_factory, ExperienceReviewRow) == 0
    audit = await commands(database_session_factory)
    assert len(audit) == 1 and audit[0].outcome == "error" and audit[0].error_code == code
    assert audit[0].authorization_id == UUID(response.headers["x-authorization-id"])
    assert audit[0].http_status == 409 and audit[0].review_id is None


async def test_idempotency_conflict_and_terminal_reversal_are_audited(
    experience_context,
    database_session_factory,
):
    candidate, _ = await candidate_ready(experience_context, database_session_factory)
    _, client, _ = experience_context
    confirmed = await submit(client, candidate)
    assert confirmed.status_code == 201, confirmed.text
    changed = await submit(client, candidate, command_for(candidate, reason="换了审核意见"))
    reversed_result = await submit(
        client,
        candidate,
        command_for(
            candidate,
            idempotency_key="another-command",
            decision="reject",
        ),
    )
    assert changed.json()["error_code"] == "experience_review_idempotency_conflict"
    assert reversed_result.json()["error_code"] == "experience_candidate_already_reviewed"
    assert changed.status_code == reversed_result.status_code == 409
    assert await count(database_session_factory, ExperienceReviewRow) == 1
    audit = await commands(database_session_factory)
    assert len(audit) == 3 and sum(row.outcome == "error" for row in audit) == 2
    assert len({row.review_id for row in audit}) == 1


async def test_confirm_reject_race_has_one_winner_and_persisted_loser(
    experience_context,
    database_session_factory,
):
    candidate, _ = await candidate_ready(experience_context, database_session_factory)
    _, client, _ = experience_context
    responses = await asyncio.gather(
        *(
            submit(
                client,
                candidate,
                command_for(candidate, decision=decision, idempotency_key=f"race-{decision}"),
            )
            for decision in ("confirm", "reject")
        )
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    winner = next(r for r in responses if r.status_code == 201).json()
    assert await count(database_session_factory, ExperienceReviewRow) == 1
    assert await count(database_session_factory, ExperienceEntryRow) == (
        winner["entry"] is not None
    )
    audit = await commands(database_session_factory)
    assert len(audit) == 2 and {a.outcome for a in audit} == {"applied", "error"}
    assert {a.review_id for a in audit} == {UUID(winner["review"]["id"])}


async def test_duplicate_concurrent_commands_replay_one_immutable_decision(
    experience_context,
    database_session_factory,
):
    candidate, _ = await candidate_ready(experience_context, database_session_factory)
    _, client, _ = experience_context
    responses = await asyncio.gather(*(submit(client, candidate) for _ in range(3)))
    assert sorted(r.status_code for r in responses) == [200, 200, 201]
    assert len({r.json()["review"]["id"] for r in responses}) == 1
    assert await count(database_session_factory, ExperienceEntryRow) == 1
    assert len(await commands(database_session_factory)) == 3


async def test_source_drift_is_refused_with_persisted_business_failure(
    experience_context,
    database_session_factory,
):
    candidate, job = await candidate_ready(experience_context, database_session_factory)
    _, client, _ = experience_context
    async with database_session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE evidence_items SET summary = '来源变动' WHERE investigation_run_id = :id"),
            {"id": UUID(job["source"]["investigation"]["id"])},
        )
    response = await submit(client, candidate)
    assert response.status_code == 409 and response.json()["error_code"] == "source_conflict"
    assert await count(database_session_factory, ExperienceReviewRow) == 0
    assert (await commands(database_session_factory))[0].error_code == "source_conflict"


async def test_successful_replay_preserves_frozen_confirmation_after_source_drift(
    experience_context,
    database_session_factory,
):
    candidate, job = await candidate_ready(experience_context, database_session_factory)
    _, client, _ = experience_context
    original = await submit(client, candidate)
    assert original.status_code == 201
    async with database_session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE evidence_items SET summary = '来源后来变动' "
                "WHERE investigation_run_id = :id"
            ),
            {"id": UUID(job["source"]["investigation"]["id"])},
        )
    repeated = await submit(client, candidate)
    assert repeated.status_code == 200
    assert repeated.json() == {**original.json(), "applied": False}
    assert await count(database_session_factory, ExperienceEntryRow) == 1
    assert {a.outcome for a in await commands(database_session_factory)} == {"applied", "replayed"}


async def test_other_engineer_cannot_replay_original_subjects_command(
    experience_context,
    database_session_factory,
):
    candidate, _ = await candidate_ready(experience_context, database_session_factory)
    app, client, clock = experience_context
    first = await submit(client, candidate)
    assert first.status_code == 201
    # 公开固定测试凭据；两个工程师拥有相同权限但主体身份不同。
    secret = "second-engineer-test-secret-" * 4
    app.state.operator_auth_settings = OperatorAuthSettings(
        mode="bearer",
        credentials=(
            *auth_settings().credentials,
            OperatorCredential(
                credential_id="engineer2",
                subject_id="trusted-engineer-2",
                role=OperatorRole.ENGINEER,
                secret_sha256=hashlib.sha256(secret.encode()).hexdigest(),
                expires_at=clock() + timedelta(days=1),
            ),
        ),
    )
    response = await client.post(
        f"/api/v1/experience-candidates/{candidate['id']}/reviews",
        json=command_for(candidate),
        headers={"Authorization": f"Bearer engineer2.{secret}"},
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "experience_candidate_already_reviewed"
    audit = await commands(database_session_factory)
    assert {a.requested_by_id for a in audit} == {"trusted-engineer", "trusted-engineer-2"}
    assert {a.review_id for a in audit} == {UUID(first.json()["review"]["id"])}


async def test_service_refuses_authorization_for_different_business_operation(
    experience_context,
    database_session_factory,
):
    candidate, job = await candidate_ready(experience_context, database_session_factory)
    app, _, clock = experience_context
    principal = OperatorPrincipal(
        "trusted-engineer", "engineer", OperatorRole.ENGINEER, clock() + timedelta(days=1)
    )
    with pytest.raises(OperatorAccessError) as error:
        await app.state.experience_review_store.decide(
            UUID(candidate["id"]),
            ReviewCommand.model_validate(command_for(candidate)),
            principal=principal,
            authorization_id=UUID(job["authorization_id"]),
        )
    assert error.value.code == "operator_permission_denied"
    assert await count(database_session_factory, ExperienceReviewRow) == 0
    assert await count(database_session_factory, ExperienceReviewCommandRow) == 0


async def test_unknown_target_has_business_not_found_audit(
    experience_context, database_session_factory
):
    _, client, _ = experience_context
    candidate = {"id": str(uuid4()), "version": 1, "content_hash": "a" * 64}
    response = await submit(client, candidate)
    assert response.status_code == 404
    audit = await commands(database_session_factory)
    assert len(audit) == 1 and audit[0].candidate_id == UUID(candidate["id"])
    assert audit[0].error_code == "experience_candidate_not_found" and audit[0].http_status == 404
    for path in (
        f"/api/v1/experience-candidates/{uuid4()}/review",
        f"/api/v1/experience-entries/{uuid4()}",
    ):
        assert (await client.get(path, headers=auth_header("viewer"))).status_code == 404
        assert (await client.get(path, headers=auth_header("alert_source"))).status_code == 403


@pytest.mark.parametrize("table", ["experience_entries", "experience_review_commands"])
async def test_confirmation_and_business_audit_share_atomic_transaction(
    experience_context,
    database_session_factory,
    table,
):
    candidate, _ = await candidate_ready(experience_context, database_session_factory)
    app, client, _ = experience_context
    async with database_session_factory() as session, session.begin():
        await session.execute(
            text(f"""
            CREATE FUNCTION test_review_failure() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION '测试确认事务失败'; END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER test_review_failure_insert AFTER INSERT ON {table}
                FOR EACH ROW EXECUTE FUNCTION test_review_failure();
        """)
        )
    try:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://testserver",
        ) as failing:
            response = await submit(failing, candidate)
        assert response.status_code == 500 and "测试确认事务失败" not in response.text
        for row in (ExperienceReviewRow, ExperienceEntryRow, ExperienceReviewCommandRow):
            assert await count(database_session_factory, row) == 0
    finally:
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    f"DROP TRIGGER test_review_failure_insert ON {table}; "
                    "DROP FUNCTION test_review_failure();"
                )
            )
    assert (await submit(client, candidate)).status_code == 201


async def test_expired_subject_and_service_permission_checks_cannot_replay(
    experience_context,
    database_session_factory,
):
    candidate, _ = await candidate_ready(experience_context, database_session_factory)
    app, client, clock = experience_context
    first = await submit(client, candidate)
    assert first.status_code == 201
    principal = OperatorPrincipal("trusted-engineer", "engineer", OperatorRole.ENGINEER, clock())
    with pytest.raises(OperatorAccessError):
        await app.state.experience_review_store.decide(
            UUID(candidate["id"]),
            ReviewCommand.model_validate(command_for(candidate)),
            principal=principal,
            authorization_id=UUID(first.headers["x-authorization-id"]),
        )
    principal = OperatorPrincipal(
        "viewer", "viewer", OperatorRole.VIEWER, clock() + timedelta(days=1)
    )
    with pytest.raises(OperatorAccessError):
        await app.state.experience_review_store.decide(
            UUID(candidate["id"]),
            ReviewCommand.model_validate(command_for(candidate)),
            principal=principal,
            authorization_id=uuid4(),
        )
    clock.advance(24 * 3600)
    assert (await submit(client, candidate)).status_code == 401
    assert len(await commands(database_session_factory)) == 1


async def test_post_write_business_validation_rejection_keeps_only_error_audit(
    experience_context,
    database_session_factory,
    monkeypatch,
):
    candidate, _ = await candidate_ready(experience_context, database_session_factory)
    _, client, _ = experience_context

    def reject_confirmation(*args):
        raise ExperienceError("experience_entry_integrity_conflict", "测试确认工件校验拒绝")

    # 在数据库 flush 后模拟业务校验失败，结果审计仍应能独立提交。
    monkeypatch.setattr(review_store_module, "validated_entry", reject_confirmation)
    response = await submit(client, candidate)
    assert response.status_code == 409
    assert response.json()["error_code"] == "experience_entry_integrity_conflict"
    assert await count(database_session_factory, ExperienceReviewRow) == 0
    assert await count(database_session_factory, ExperienceEntryRow) == 0
    audit = await commands(database_session_factory)
    assert len(audit) == 1 and audit[0].outcome == "error" and audit[0].review_id is None
