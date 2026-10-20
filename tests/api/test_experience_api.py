"""经验生成的可信 HTTP 准入及真实 PostgreSQL 端到端闭环。"""

import asyncio
import os
import subprocess
import sys
from datetime import timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

import opsswarm.experience.store as experience_store_module
from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from opsswarm.experience.contracts import ExperienceError
from opsswarm.experience.projection import project_source
from opsswarm.experience.sources import load_source
from opsswarm.experience.tables import ExperienceCandidateRow, ExperienceJobRow
from opsswarm.experience.worker import ExperienceWorker
from opsswarm.identity.settings import OperatorAuthSettings
from tests.api.test_control_plane_api import (
    ApiOperationAdapter,
    ApiVerificationAdapter,
    alert_payload,
    resolve_incident,
)
from tests.experience_helpers import ExperienceClock, completed_source_report
from tests.identity_helpers import auth_header, auth_settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def experience_context(database_session_factory, migrated_database_url):
    clock = ExperienceClock()
    app = create_app(
        database_settings=DatabaseSettings(database_url=migrated_database_url),
        operator_auth_settings=auth_settings(),
        clock=clock,
        operation_adapter=ApiOperationAdapter(),
        verification_adapter=ApiVerificationAdapter(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        yield app, client, clock


async def create_job(context, factory):
    app, client, clock = context
    report = await completed_source_report(client, factory, clock)
    response = await client.post(
        f"/api/v1/incident-reports/{report['id']}/experience-generation-jobs",
        json={"expected_report_content_hash": report["content_hash"]},
        headers=auth_header("engineer"),
    )
    assert response.status_code == 201, response.text
    return app.state.experience_store, response.json()["job"], report


async def test_report_job_worker_candidate_full_chain(experience_context, database_session_factory):
    store, job, report = await create_job(experience_context, database_session_factory)
    _, client, _ = experience_context
    assert job["status"] == "queued" and job["attempts"] == []
    assert job["requested_by_id"] == "trusted-engineer"
    async with database_session_factory() as session:
        source = await load_source(session, UUID(report["id"]))
    worker = ExperienceWorker(store, worker_id="experience-worker-1")
    assert await worker.run_once() is True
    assert await worker.run_once() is False
    fetched = await client.get(
        f"/api/v1/experience-generation-jobs/{job['id']}", headers=auth_header("viewer")
    )
    assert fetched.status_code == 200, fetched.text
    completed = fetched.json()
    assert completed["status"] == "succeeded" and completed["attempt_count"] == 1
    assert completed["source"] == job["source"]
    assert "lease_token" not in completed
    assert all("lease_token" not in attempt for attempt in completed["attempts"])
    assert completed["source"]["execution"]["lease_token"] is None
    candidate = await client.get(
        f"/api/v1/experience-candidates/{completed['candidate_id']}", headers=auth_header("viewer")
    )
    assert candidate.status_code == 200, candidate.text
    assert candidate.json()["status"] == "pending_review"
    assert candidate.json()["content"] == project_source(source).model_dump(mode="json")
    assert candidate.json()["source_hash"] == job["source_hash"]
    repeat = await client.post(
        f"/api/v1/incident-reports/{report['id']}/experience-generation-jobs",
        json={"expected_report_content_hash": report["content_hash"]},
        headers=auth_header("engineer"),
    )
    assert repeat.status_code == 200 and repeat.json()["created"] is False
    assert repeat.json()["job"]["authorization_id"] == job["authorization_id"]


async def test_concurrent_requests_create_one_job_and_stale_hash_conflicts(
    experience_context,
    database_session_factory,
):
    _, client, clock = experience_context
    report = await completed_source_report(client, database_session_factory, clock)
    path = f"/api/v1/incident-reports/{report['id']}/experience-generation-jobs"
    responses = await asyncio.gather(
        *(
            client.post(
                path,
                json={"expected_report_content_hash": report["content_hash"]},
                headers=auth_header("engineer"),
            )
            for _ in range(3)
        )
    )
    assert sorted(r.status_code for r in responses) == [200, 200, 201]
    assert len({r.json()["job"]["id"] for r in responses}) == 1
    conflict = await client.post(
        path, json={"expected_report_content_hash": "0" * 64}, headers=auth_header("engineer")
    )
    assert conflict.status_code == 409 and conflict.json()["error_code"] == "source_conflict"
    forged = await client.post(
        path,
        json={"expected_report_content_hash": report["content_hash"], "requested_by_id": "forged"},
        headers=auth_header("engineer"),
    )
    assert forged.status_code == 422
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ExperienceJobRow)) == 1


@pytest.mark.parametrize("role", [None, "viewer", "approver", "executor", "alert_source"])
async def test_generation_requires_engineer_even_for_missing_report(experience_context, role):
    _, client, _ = experience_context
    response = await client.post(
        f"/api/v1/incident-reports/{uuid4()}/experience-generation-jobs",
        json={"expected_report_content_hash": "0" * 64},
        headers=auth_header(role) if role else {},
    )
    assert response.status_code == (403 if role else 401)


async def test_new_experience_routes_require_bearer_in_legacy_mode(
    database_session_factory,
    migrated_database_url,
):
    app = create_app(
        database_settings=DatabaseSettings(database_url=migrated_database_url),
        operator_auth_settings=OperatorAuthSettings(mode="legacy_local"),
    )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        for path in (
            f"/api/v1/experience-generation-jobs/{uuid4()}",
            f"/api/v1/experience-candidates/{uuid4()}",
        ):
            assert (await client.get(path)).status_code == 401
        assert (
            await client.post(
                f"/api/v1/incident-reports/{uuid4()}/experience-generation-jobs", json={}
            )
        ).status_code == 401


async def test_missing_entities_return_safe_not_found(experience_context):
    _, client, _ = experience_context
    for path in (
        f"/api/v1/experience-generation-jobs/{uuid4()}",
        f"/api/v1/experience-candidates/{uuid4()}",
    ):
        assert (await client.get(path, headers=auth_header("viewer"))).status_code == 404
        assert (await client.get(path, headers=auth_header("alert_source"))).status_code == 403
    missing = await client.post(
        f"/api/v1/incident-reports/{uuid4()}/experience-generation-jobs",
        json={"expected_report_content_hash": "0" * 64},
        headers=auth_header("engineer"),
    )
    assert missing.status_code == 404


async def test_worker_competition_lease_recovery_and_late_results(
    experience_context,
    database_session_factory,
):
    store, job, _ = await create_job(experience_context, database_session_factory)
    _, _, clock = experience_context
    claims = await asyncio.gather(
        *(
            store.claim_next(worker_id=f"worker-{i}", lease_duration=timedelta(seconds=2))
            for i in range(4)
        )
    )
    assert sum(claim is not None for claim in claims) == 1
    old = next(claim for claim in claims if claim is not None)
    draft = project_source(await store.get_claim_source(old))
    clock.advance(2)
    new = await store.claim_next(worker_id="new-worker")
    assert new is not None and new.job_id == old.job_id and new.attempt_id != old.attempt_id
    with pytest.raises(ExperienceError, match="当前有效尝试"):
        await store.complete(old, draft)
    with pytest.raises(ExperienceError):
        await store.fail(old, error_code="old_failure", retryable=False)
    candidate = await store.complete(new, draft)
    assert await store.complete(new, draft) == candidate
    with pytest.raises(ExperienceError):
        await store.complete(old, draft)
    result = await store.get(UUID(job["id"]))
    assert [a.status for a in result.attempts] == ["lease_expired", "succeeded"]
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ExperienceCandidateRow)) == 1


async def test_failures_retry_with_stable_job_and_bounded_attempts(
    experience_context,
    database_session_factory,
):
    store, job, _ = await create_job(experience_context, database_session_factory)
    _, _, clock = experience_context

    def failing_projector(source):
        raise RuntimeError("秘密不得写入持久错误或响应")

    worker = ExperienceWorker(store, worker_id="retry-worker", projector=failing_projector)
    for count in range(1, 4):
        assert await worker.run_once() is True
        result = await store.get(UUID(job["id"]))
        assert result.attempt_count == count
        assert result.last_error_code == "experience_generation_failed"
        assert "秘密" not in result.model_dump_json()
        assert result.status == ("failed" if count == 3 else "queued")
        assert await worker.run_once() is False
        clock.advance(2**count)


async def test_three_crashed_attempts_exhaust_without_candidate(
    experience_context,
    database_session_factory,
):
    store, job, _ = await create_job(experience_context, database_session_factory)
    _, _, clock = experience_context
    for _ in range(3):
        assert await store.claim_next(worker_id="crashed", lease_duration=timedelta(seconds=1))
        clock.advance(1)
    assert await store.claim_next(worker_id="recovery") is None
    result = await store.get(UUID(job["id"]))
    assert result.status == "failed" and result.last_error_code == "attempts_exhausted"
    assert {a.status for a in result.attempts} == {"lease_expired"}


async def test_forged_projection_rejected_and_deadline_rechecked(
    experience_context,
    database_session_factory,
):
    store, job, _ = await create_job(experience_context, database_session_factory)
    _, _, clock = experience_context
    claim = await store.claim_next(worker_id="worker", lease_duration=timedelta(seconds=1))
    draft = project_source(await store.get_claim_source(claim))
    with pytest.raises(ExperienceError) as rejected:
        await store.complete(claim, draft.model_copy(update={"historical_summary": "伪造经验"}))
    assert rejected.value.code == "candidate_conflict"
    clock.advance(1)
    with pytest.raises(ExperienceError) as expired:
        await store.complete(claim, draft)
    assert expired.value.code == "experience_lease_lost"
    assert (await store.get(UUID(job["id"]))).candidate_id is None


async def test_source_row_drift_fails_worker_without_candidate(
    experience_context,
    database_session_factory,
):
    store, job, _ = await create_job(experience_context, database_session_factory)
    async with database_session_factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE evidence_items SET summary = '来源变动' "
                "WHERE investigation_run_id = :run_id"
            ),
            {"run_id": UUID(job["source"]["investigation"]["id"])},
        )
    assert await ExperienceWorker(store, worker_id="drift-worker").run_once()
    result = await store.get(UUID(job["id"]))
    assert result.status == "failed" and result.last_error_code == "source_conflict"
    assert result.candidate_id is None


async def test_incomplete_closed_report_does_not_fabricate_experience(
    experience_context,
    database_session_factory,
):
    _, client, _ = experience_context
    response = await client.post(
        "/api/v1/alerts",
        json=alert_payload("incomplete-source"),
        headers=auth_header("alert_source"),
    )
    incident_id = response.json()["incident"]["id"]
    resolved = await resolve_incident(database_session_factory, incident_id)
    closed = await client.post(
        f"/api/v1/incidents/{incident_id}/actions/close",
        headers=auth_header("engineer"),
        json={"expected_version": resolved.incident.version, "reason": "仅状态关闭"},
    )
    assert closed.status_code == 200, closed.text
    response = await client.post(
        f"/api/v1/incidents/{incident_id}/reports",
        headers=auth_header("engineer"),
        json={"expected_incident_version": closed.json()["version"]},
    )
    report = response.json()["report"]
    rejected = await client.post(
        f"/api/v1/incident-reports/{report['id']}/experience-generation-jobs",
        headers=auth_header("engineer"),
        json={"expected_report_content_hash": report["content_hash"]},
    )
    assert rejected.status_code == 409 and rejected.json()["error_code"] == "source_not_ready"
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ExperienceJobRow)) == 0


async def test_independent_worker_command_persists_result_after_process_exit(
    experience_context,
    database_session_factory,
    migrated_database_url,
):
    store, job, _ = await create_job(experience_context, database_session_factory)
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "opsswarm.experience.main", "--once"],
        env={**os.environ, "OPSSWARM_DATABASE_URL": migrated_database_url},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    persisted = await store.get(UUID(job["id"]))
    assert persisted.status == "succeeded" and persisted.candidate_id is not None
    assert (await store.get_candidate(persisted.candidate_id)).status == "pending_review"


async def test_database_insert_failure_rolls_back_candidate_and_success_together(
    experience_context,
    database_session_factory,
):
    store, job, _ = await create_job(experience_context, database_session_factory)
    _, _, clock = experience_context
    async with database_session_factory() as session, session.begin():
        await session.execute(
            text("""
            CREATE FUNCTION test_reject_candidate() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION '测试候选事务失败'; END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER test_reject_candidate_insert AFTER INSERT ON experience_candidates
                FOR EACH ROW EXECUTE FUNCTION test_reject_candidate();
        """)
        )
    try:
        assert await ExperienceWorker(store, worker_id="failed-transaction").run_once()
        result = await store.get(UUID(job["id"]))
        assert result.status == "queued" and result.candidate_id is None
        assert result.attempts[0].status == "failed"
        assert result.last_error_code == "experience_generation_failed"
        async with database_session_factory() as session:
            assert (
                await session.scalar(select(func.count()).select_from(ExperienceCandidateRow)) == 0
            )
    finally:
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "DROP TRIGGER test_reject_candidate_insert ON experience_candidates; "
                    "DROP FUNCTION test_reject_candidate();"
                )
            )
    clock.advance(2)
    assert await ExperienceWorker(store, worker_id="recovered-transaction").run_once()
    result = await store.get(UUID(job["id"]))
    assert result.status == "succeeded" and result.attempt_count == 2


@pytest.mark.parametrize("initial_status", ["queued", "running"])
async def test_read_snapshot_remains_consistent_during_claim_or_completion(
    experience_context,
    database_session_factory,
    monkeypatch,
    initial_status,
):
    store, job, _ = await create_job(experience_context, database_session_factory)
    claim = draft = None
    if initial_status == "running":
        claim = await store.claim_next(worker_id="snapshot-worker")
        draft = project_source(await store.get_claim_source(claim))
    ready, release = asyncio.Event(), asyncio.Event()
    original = experience_store_module._job_view

    async def pause_after_job_read(session, row):
        ready.set()
        await release.wait()
        return await original(session, row)

    monkeypatch.setattr(experience_store_module, "_job_view", pause_after_job_read)
    reader = asyncio.create_task(store.get(UUID(job["id"])))
    await asyncio.wait_for(ready.wait(), timeout=5)
    writer = asyncio.create_task(
        store.claim_next(worker_id="concurrent-claim")
        if initial_status == "queued"
        else store.complete(claim, draft)
    )
    blocked = False
    try:
        await asyncio.wait_for(asyncio.shield(writer), timeout=0.3)
    except TimeoutError:
        blocked = True
    finally:
        release.set()
    snapshot = await asyncio.wait_for(reader, timeout=5)
    result = await asyncio.wait_for(writer, timeout=5)
    if initial_status == "queued":
        # SKIP LOCKED 应跳过正在查询的任务，下一次轮询仍可领取。
        assert not blocked and result is None
        assert await store.claim_next(worker_id="after-read") is not None
    else:
        assert blocked
    assert snapshot.status == initial_status and snapshot.candidate_id is None
    assert snapshot.attempt_count == (0 if initial_status == "queued" else 1)
