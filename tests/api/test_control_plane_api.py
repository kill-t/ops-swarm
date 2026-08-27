"""控制面 API 与真实 PostgreSQL 的集成测试。"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from opsswarm.evaluation import MVP_INVESTIGATION_DATASET_V1
from opsswarm.evaluation.fixtures import run_fixture_benchmark
from opsswarm.evaluation.persistence import EvaluationRunSubmission, EvaluationStore
from opsswarm.execution import OperationResult
from opsswarm.incidents import ActorType, IncidentAction, IncidentActor
from opsswarm.incidents.persistence import IncidentStore, StoredIncident
from opsswarm.investigation.challenge import challenge_investigation
from opsswarm.investigation.models import (
    InvestigationArtifacts,
    InvestigationConclusion,
    InvestigationMetrics,
    InvestigationRunResult,
    SpecialistReport,
)
from opsswarm.investigation.persistence import InvestigationStore
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
from opsswarm.planning.persistence import StoredActionStep
from opsswarm.verification import VerificationObservation

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


class ApiOperationAdapter:
    """API 集成测试不依赖真实网络，但保持相同白名单结果契约。"""

    async def execute(self, step: StoredActionStep) -> OperationResult:
        return OperationResult(
            output={
                "fault": "release_500",
                "enabled": False,
                "service": step.target_name,
            }
        )


class ApiVerificationAdapter:
    """API 集成测试用固定健康探针，不访问真实网络。"""

    async def observe(self, step: StoredActionStep) -> VerificationObservation:
        return VerificationObservation(
            signal=step.verification.signal,
            value=0.0,
            http_status=200,
        )


def fixed_clock() -> datetime:
    return BASE_TIME


async def resolve_incident(
    session_factory: async_sessionmaker[AsyncSession],
    incident_id: str,
) -> StoredIncident:
    """通过正式仓储把 API 测试事故推进到可关闭状态。"""

    store = IncidentStore(session_factory)
    actor = IncidentActor(actor_type=ActorType.HUMAN, actor_id="engineer-001")
    version = 1
    for action in (
        IncidentAction.START_INVESTIGATION,
        IncidentAction.START_VERIFICATION,
        IncidentAction.CONFIRM_RECOVERY,
    ):
        stored = await store.transition(
            UUID(incident_id),
            action=action,
            expected_version=version,
            occurred_at=BASE_TIME,
            actor=actor,
            reason="API 测试推进到已恢复状态",
        )
        version = stored.incident.version
    return stored


def alert_payload(
    external_event_id: str,
    *,
    fingerprint: str = "order-service:http-500",
) -> dict[str, object]:
    return {
        "source": "fault-simulator",
        "external_event_id": external_event_id,
        "fingerprint": fingerprint,
        "service_name": "order-service",
        "severity": "p1",
        "title": "订单接口大量返回 500",
        "payload": {"status_code": 500},
        "occurred_at": "2026-01-01T07:59:59+00:00",
    }


@pytest_asyncio.fixture
async def control_client(
    migrated_database_url: str,
    database_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx2.AsyncClient]:
    """通过正式应用生命周期创建和释放数据库连接池。"""

    _ = database_session_factory
    app = create_app(
        database_settings=DatabaseSettings(database_url=migrated_database_url),
        clock=fixed_clock,
        operation_adapter=ApiOperationAdapter(),
        verification_adapter=ApiVerificationAdapter(),
    )
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client


async def test_health_endpoint(control_client: httpx2.AsyncClient) -> None:
    response = await control_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "service": "opsswarm-control-plane",
        "status": "healthy",
    }


async def test_evaluation_runs_are_read_only_and_cursor_paginated(
    control_client: httpx2.AsyncClient,
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    report = await run_fixture_benchmark()
    creation = await EvaluationStore(database_session_factory).save_report(
        EvaluationRunSubmission(
            dataset=MVP_INVESTIGATION_DATASET_V1,
            report=report,
            idempotency_key="evaluation-api-001",
            runner_name="fixture-contract-v1",
            created_at=report.generated_at,
        )
    )

    page = await control_client.get("/api/v1/evaluation-runs", params={"limit": 1})
    detail = await control_client.get(f"/api/v1/evaluation-runs/{creation.run.id}")

    assert page.status_code == 200
    assert len(page.json()["items"]) == 1
    assert page.json()["items"][0]["id"] == str(creation.run.id)
    assert page.json()["items"][0]["dataset_key"] == MVP_INVESTIGATION_DATASET_V1.dataset_key
    assert page.json()["next_before_sequence"] is None
    assert detail.status_code == 200
    assert detail.json()["idempotency_key"] == "evaluation-api-001"
    assert (
        detail.json()["report"]["scenario_results"]
        == report.model_dump(mode="json")["scenario_results"]
    )


async def test_missing_evaluation_run_returns_structured_404(
    control_client: httpx2.AsyncClient,
) -> None:
    run_id = uuid4()

    response = await control_client.get(f"/api/v1/evaluation-runs/{run_id}")

    assert response.status_code == 404
    assert response.json() == {
        "error_code": "evaluation_run_not_found",
        "message": f"评测运行不存在：{run_id}",
        "details": {"run_id": str(run_id)},
    }


async def test_new_alert_returns_201_and_persisted_incident(
    control_client: httpx2.AsyncClient,
) -> None:
    response = await control_client.post("/api/v1/alerts", json=alert_payload("alert-001"))

    assert response.status_code == 201
    body = response.json()
    assert body["deduplicated"] is False
    assert body["created_incident"] is True
    assert body["incident"]["reference"] == "INC-000001"
    assert body["incident"]["status"] == "open"
    assert body["incident"]["version"] == 1
    assert body["incident"]["created_at"] == "2026-01-01T08:00:00Z"

    detail = await control_client.get(f"/api/v1/incidents/{body['incident']['id']}")
    assert detail.status_code == 200
    assert detail.json() == body["incident"]

    workroom = await control_client.get(f"/api/v1/incidents/{body['incident']['id']}/workroom")
    assert workroom.status_code == 200
    assert workroom.json() == {
        "incident_id": body["incident"]["id"],
        "incident_version": 1,
        "investigation": None,
        "evidence_board": None,
        "action_plan": None,
        "approval_request": None,
        "operation_execution": None,
        "verification_run": None,
    }


async def test_duplicate_alert_returns_200_and_original_identifiers(
    control_client: httpx2.AsyncClient,
) -> None:
    payload = alert_payload("alert-001")

    first = await control_client.post("/api/v1/alerts", json=payload)
    duplicate = await control_client.post("/api/v1/alerts", json=payload)

    assert first.status_code == 201
    assert duplicate.status_code == 200
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["alert_id"] == first.json()["alert_id"]
    assert duplicate.json()["incident"]["id"] == first.json()["incident"]["id"]


async def test_same_fingerprint_new_event_returns_201_and_attaches_timeline(
    control_client: httpx2.AsyncClient,
) -> None:
    first = await control_client.post(
        "/api/v1/alerts",
        json=alert_payload("alert-001"),
    )
    attached = await control_client.post(
        "/api/v1/alerts",
        json=alert_payload("alert-002"),
    )

    assert attached.status_code == 201
    assert attached.json()["created_incident"] is False
    assert attached.json()["deduplicated"] is False
    assert attached.json()["incident"]["id"] == first.json()["incident"]["id"]

    events = await control_client.get(f"/api/v1/incidents/{first.json()['incident']['id']}/events")
    assert [item["event_type"] for item in events.json()["items"]] == [
        "incident.created",
        "incident.alert_attached",
    ]


async def test_new_alert_reopens_resolved_incident(
    control_client: httpx2.AsyncClient,
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await control_client.post("/api/v1/alerts", json=alert_payload("alert-001"))
    incident_id = first.json()["incident"]["id"]
    await resolve_incident(database_session_factory, incident_id)

    recurrence = await control_client.post(
        "/api/v1/alerts",
        json=alert_payload("alert-002"),
    )
    events = await control_client.get(f"/api/v1/incidents/{incident_id}/events")

    assert recurrence.status_code == 201
    assert recurrence.json()["created_incident"] is False
    assert recurrence.json()["incident"]["id"] == incident_id
    assert recurrence.json()["incident"]["status"] == "investigating"
    assert recurrence.json()["incident"]["version"] == 5
    assert recurrence.json()["incident"]["resolved_at"] is None
    assert [item["event_type"] for item in events.json()["items"][-2:]] == [
        "incident.alert_attached",
        "incident.status_changed",
    ]
    assert events.json()["items"][-1]["payload"] == {"action": "reopen"}


async def test_engineer_closes_resolved_incident_and_recurrence_creates_new_incident(
    control_client: httpx2.AsyncClient,
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await control_client.post("/api/v1/alerts", json=alert_payload("alert-001"))
    incident_id = first.json()["incident"]["id"]
    resolved = await resolve_incident(database_session_factory, incident_id)

    closed = await control_client.post(
        f"/api/v1/incidents/{incident_id}/actions/close",
        json={
            "expected_version": resolved.incident.version,
            "actor_id": "engineer-001",
            "reason": "恢复窗口已经通过，确认关闭",
        },
    )
    recurrence = await control_client.post(
        "/api/v1/alerts",
        json=alert_payload("alert-002"),
    )
    old_incident = await control_client.get(f"/api/v1/incidents/{incident_id}")
    old_events = await control_client.get(f"/api/v1/incidents/{incident_id}/events")

    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    assert closed.json()["version"] == 5
    assert closed.json()["closed_at"] == "2026-01-01T08:00:00Z"
    assert recurrence.status_code == 201
    assert recurrence.json()["created_incident"] is True
    assert recurrence.json()["incident"]["id"] != incident_id
    assert recurrence.json()["incident"]["reference"] == "INC-000002"
    assert old_incident.json() == closed.json()
    assert old_events.json()["items"][-1]["actor_id"] == "engineer-001"
    assert old_events.json()["items"][-1]["reason"] == "恢复窗口已经通过，确认关闭"
    assert old_events.json()["items"][-1]["to_status"] == "closed"


async def test_close_rejects_unresolved_incident_and_stale_version(
    control_client: httpx2.AsyncClient,
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await control_client.post("/api/v1/alerts", json=alert_payload("alert-001"))
    incident_id = created.json()["incident"]["id"]
    close_url = f"/api/v1/incidents/{incident_id}/actions/close"
    body = {
        "expected_version": 1,
        "actor_id": "engineer-001",
        "reason": "尝试关闭",
    }

    unresolved = await control_client.post(close_url, json=body)
    resolved = await resolve_incident(database_session_factory, incident_id)
    stale = await control_client.post(close_url, json=body)

    assert unresolved.status_code == 409
    assert unresolved.json()["error_code"] == "invalid_incident_transition"
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "incident_version_conflict"
    assert stale.json()["details"] == {
        "expected_version": 1,
        "actual_version": resolved.incident.version,
    }


async def test_incident_list_uses_stable_descending_cursor(
    control_client: httpx2.AsyncClient,
) -> None:
    for number in range(1, 4):
        response = await control_client.post(
            "/api/v1/alerts",
            json=alert_payload(
                f"alert-{number:03d}",
                fingerprint=f"order-service:http-500:{number}",
            ),
        )
        assert response.status_code == 201

    first_page = await control_client.get("/api/v1/incidents", params={"limit": 2})

    assert first_page.status_code == 200
    assert [item["display_number"] for item in first_page.json()["items"]] == [3, 2]
    assert first_page.json()["next_before_display_number"] == 2

    second_page = await control_client.get(
        "/api/v1/incidents",
        params={"limit": 2, "before_display_number": 2},
    )
    assert [item["display_number"] for item in second_page.json()["items"]] == [1]
    assert second_page.json()["next_before_display_number"] is None


async def test_start_investigation_and_status_filter(
    control_client: httpx2.AsyncClient,
) -> None:
    created = await control_client.post(
        "/api/v1/alerts",
        json=alert_payload("alert-001"),
    )
    incident_id = created.json()["incident"]["id"]

    started = await control_client.post(
        f"/api/v1/incidents/{incident_id}/actions/start-investigation",
        json={
            "expected_version": 1,
            "actor_id": "engineer-001",
            "reason": "值班工程师开始调查",
        },
    )

    assert started.status_code == 200
    started_body = started.json()
    assert started_body["incident"]["status"] == "investigating"
    assert started_body["incident"]["version"] == 2
    assert started_body["investigation"]["status"] == "queued"
    assert started_body["investigation"]["mode"] == "single_agent"
    assert started_body["work_item_status"] == "pending"

    run_response = await control_client.get(
        f"/api/v1/investigations/{started_body['investigation']['id']}"
    )
    assert run_response.status_code == 200
    assert run_response.json() == started_body["investigation"]

    filtered = await control_client.get(
        "/api/v1/incidents",
        params={"status": "investigating"},
    )
    assert [item["id"] for item in filtered.json()["items"]] == [incident_id]

    plan_response = await control_client.post(
        f"/api/v1/investigations/{started_body['investigation']['id']}/action-plans"
    )
    assert plan_response.status_code == 409
    assert plan_response.json()["error_code"] == "action_plan_not_ready"

    missing_plan = await control_client.get(
        f"/api/v1/investigations/{started_body['investigation']['id']}/action-plans/current"
    )
    assert missing_plan.status_code == 404
    assert missing_plan.json()["error_code"] == "action_plan_not_found"


async def test_start_multi_agent_investigation_persists_graph_thread(
    control_client: httpx2.AsyncClient,
) -> None:
    created = await control_client.post(
        "/api/v1/alerts",
        json=alert_payload("alert-multi-001"),
    )
    incident_id = created.json()["incident"]["id"]

    started = await control_client.post(
        f"/api/v1/incidents/{incident_id}/actions/start-investigation",
        json={
            "expected_version": 1,
            "actor_id": "engineer-001",
            "reason": "使用并行调查模式",
            "mode": "multi_agent",
        },
    )

    assert started.status_code == 200
    investigation = started.json()["investigation"]
    assert investigation["mode"] == "multi_agent"
    assert investigation["prompt_version"] == "multi-agent-parallel-v1"
    assert investigation["graph_thread_id"] == f"investigation-run:{investigation['id']}"


async def test_completed_investigation_exposes_normalized_evidence_board(
    control_client: httpx2.AsyncClient,
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await control_client.post(
        "/api/v1/alerts",
        json=alert_payload("alert-evidence-board"),
    )
    started = await control_client.post(
        f"/api/v1/incidents/{created.json()['incident']['id']}/actions/start-investigation",
        json={
            "expected_version": 1,
            "actor_id": "engineer-001",
            "reason": "验证 Evidence Board 查询",
            "mode": "multi_agent",
        },
    )
    run_id = started.json()["investigation"]["id"]
    store = InvestigationStore(database_session_factory)
    claimed = await store.claim_next(
        worker_id="api-evidence-test-worker",
        occurred_at=BASE_TIME + timedelta(seconds=1),
        lease_duration=timedelta(seconds=30),
    )
    assert claimed is not None and claimed.work_item.lease_token is not None
    evidence_ids = tuple(sorted(RELEASE_500_SCENARIO.required_evidence_ids))
    result = InvestigationRunResult(
        conclusion=InvestigationConclusion(
            root_cause="release_500_regression",
            confidence=0.96,
            summary="三类证据共同指向版本回归。",
            evidence_ids=evidence_ids,
            recommendation="rollback_release",
            requires_human_approval=True,
        ),
        returned_evidence_ids=evidence_ids,
        tool_call_count=3,
    )
    reports = tuple(
        SpecialistReport(
            specialist=item.kind,
            summary=f"{item.kind.value} 专业报告",
            findings=(item.summary,),
            evidence_ids=(item.evidence_id,),
            confidence=0.9,
        )
        for item in RELEASE_500_SCENARIO.evidence
    )
    challenge = challenge_investigation(
        InvestigationArtifacts(
            result=result,
            evidence_items=RELEASE_500_SCENARIO.evidence,
            specialist_reports=reports,
        )
    )
    await store.complete(
        claimed.work_item.id,
        lease_token=claimed.work_item.lease_token,
        occurred_at=BASE_TIME + timedelta(seconds=2),
        result=result,
        evidence_items=RELEASE_500_SCENARIO.evidence,
        specialist_reports=reports,
        hypothesis_challenge=challenge,
        metrics=InvestigationMetrics(
            top1_accuracy=1,
            evidence_recall=1,
            recommendation_accuracy=1,
            safety_violation_rate=0,
            tool_call_count=3,
        ),
        model_name="fake-evidence-model",
    )

    response = await control_client.get(f"/api/v1/investigations/{run_id}/evidence-board")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["conclusion"]["root_cause"] == "release_500_regression"
    assert [item["evidence_id"] for item in body["evidence_items"]] == list(evidence_ids)
    assert [report["specialist"] for report in body["specialist_reports"]] == [
        "change",
        "log",
        "metric",
    ]
    assert all(len(item["content_hash"]) == 64 for item in body["evidence_items"])
    assert len(body["hypotheses"]) == 1
    assert body["hypotheses"][0]["status"] == "supported"
    assert body["hypotheses"][0]["rule_version"] == "hypothesis-challenge-v1"
    assert {item["relation"] for item in body["hypotheses"][0]["evidence_assessments"]} == {
        "supports"
    }

    created_plan = await control_client.post(f"/api/v1/investigations/{run_id}/action-plans")
    assert created_plan.status_code == 201
    plan_body = created_plan.json()
    assert plan_body["created"] is True
    assert plan_body["action_plan"]["version"] == 1
    assert plan_body["action_plan"]["status"] == "draft"
    assert plan_body["action_plan"]["risk_level"] == "high"
    assert plan_body["action_plan"]["requires_human_approval"] is True
    assert plan_body["action_plan"]["risk_policy_version"] == "action-risk-v1"
    assert plan_body["action_plan"]["created_at"] == body["hypotheses"][0]["created_at"]
    assert plan_body["action_plan"]["steps"][0]["action_type"] == "rollback_release"
    assert plan_body["action_plan"]["steps"][0]["parameters"] == {
        "current_version": "2026.01.01-rc1",
        "target_version": "stable",
    }

    duplicate_plan = await control_client.post(f"/api/v1/investigations/{run_id}/action-plans")
    assert duplicate_plan.status_code == 200
    assert duplicate_plan.json() == {**plan_body, "created": False}
    current_plan = await control_client.get(f"/api/v1/investigations/{run_id}/action-plans/current")
    assert current_plan.status_code == 200
    assert current_plan.json() == plan_body["action_plan"]

    incident = await control_client.get(f"/api/v1/incidents/{created.json()['incident']['id']}")
    assert incident.json()["status"] == "investigating"
    assert incident.json()["version"] == 2

    plan_id = plan_body["action_plan"]["id"]
    approval_payload = {
        "expected_incident_version": 2,
        "actor_id": "engineer-001",
        "reason": "审批高风险回滚计划",
    }
    approval = await control_client.post(
        f"/api/v1/action-plans/{plan_id}/approval-requests",
        json=approval_payload,
    )
    assert approval.status_code == 201
    approval_body = approval.json()
    assert approval_body["created"] is True
    assert approval_body["approval_request"]["action_plan_id"] == plan_id
    assert approval_body["approval_request"]["action_plan_version"] == 1
    assert approval_body["approval_request"]["status"] == "pending"
    assert approval_body["approval_request"]["requested_by_type"] == "human"
    assert approval_body["approval_request"]["decided_at"] is None
    assert len(approval_body["approval_request"]["plan_content_hash"]) == 64

    duplicate_approval = await control_client.post(
        f"/api/v1/action-plans/{plan_id}/approval-requests",
        json=approval_payload,
    )
    assert duplicate_approval.status_code == 200
    assert duplicate_approval.json() == {**approval_body, "created": False}
    request_id = approval_body["approval_request"]["id"]
    current_approval = await control_client.get(f"/api/v1/approval-requests/{request_id}")
    assert current_approval.status_code == 200
    assert current_approval.json() == approval_body["approval_request"]

    current_plan = await control_client.get(f"/api/v1/investigations/{run_id}/action-plans/current")
    assert current_plan.json()["status"] == "awaiting_approval"
    incident = await control_client.get(f"/api/v1/incidents/{created.json()['incident']['id']}")
    assert incident.json()["status"] == "awaiting_approval"
    assert incident.json()["version"] == 3

    decision_payload = {
        "expected_incident_version": 3,
        "actor_id": "engineer-002",
        "reason": "确认回滚目标和恢复判据",
    }
    approved = await control_client.post(
        f"/api/v1/approval-requests/{request_id}/actions/approve",
        json=decision_payload,
    )
    assert approved.status_code == 200
    approved_body = approved.json()
    assert approved_body["decision_applied"] is True
    assert approved_body["approval_request"]["status"] == "approved"
    assert approved_body["approval_request"]["decided_by_type"] == "human"
    assert approved_body["approval_request"]["decided_by_id"] == "engineer-002"

    duplicate_decision = await control_client.post(
        f"/api/v1/approval-requests/{request_id}/actions/approve",
        json=decision_payload,
    )
    assert duplicate_decision.status_code == 200
    assert duplicate_decision.json() == {**approved_body, "decision_applied": False}
    conflicting_decision = await control_client.post(
        f"/api/v1/approval-requests/{request_id}/actions/reject",
        json=decision_payload,
    )
    assert conflicting_decision.status_code == 409
    assert conflicting_decision.json()["error_code"] == "approval_request_already_decided"

    current_plan = await control_client.get(f"/api/v1/investigations/{run_id}/action-plans/current")
    assert current_plan.json()["status"] == "approved"
    incident = await control_client.get(f"/api/v1/incidents/{created.json()['incident']['id']}")
    assert incident.json()["status"] == "mitigating"
    assert incident.json()["version"] == 4

    step_id = plan_body["action_plan"]["steps"][0]["id"]
    execution_payload = {
        "idempotency_key": "api-execution-001",
        "expected_incident_version": 4,
    }
    execution = await control_client.post(
        f"/api/v1/action-steps/{step_id}/executions",
        json=execution_payload,
    )
    assert execution.status_code == 201
    execution_body = execution.json()
    assert execution_body["created"] is True
    assert execution_body["execution_performed"] is True
    assert execution_body["operation_execution"]["status"] == "succeeded"
    assert execution_body["operation_execution"]["result"]["enabled"] is False
    execution_id = execution_body["operation_execution"]["id"]

    duplicate_execution = await control_client.post(
        f"/api/v1/action-steps/{step_id}/executions",
        json=execution_payload,
    )
    assert duplicate_execution.status_code == 200
    assert duplicate_execution.json() == {
        **execution_body,
        "created": False,
        "execution_performed": False,
    }
    stored_execution = await control_client.get(f"/api/v1/operation-executions/{execution_id}")
    assert stored_execution.status_code == 200
    assert stored_execution.json() == execution_body["operation_execution"]
    repeated_with_new_key = await control_client.post(
        f"/api/v1/action-steps/{step_id}/executions",
        json={**execution_payload, "idempotency_key": "api-execution-002"},
    )
    assert repeated_with_new_key.status_code == 409
    assert repeated_with_new_key.json()["error_code"] == "operation_step_already_succeeded"

    incident = await control_client.get(f"/api/v1/incidents/{created.json()['incident']['id']}")
    assert incident.json()["status"] == "verifying"
    assert incident.json()["version"] == 5

    verification = await control_client.post(
        f"/api/v1/operation-executions/{execution_id}/verification-runs",
        json={"expected_incident_version": 5},
    )
    assert verification.status_code == 201
    verification_body = verification.json()
    assert verification_body["created"] is True
    assert verification_body["verification_run"]["status"] == "running"
    assert verification_body["verification_run"]["rule"] == {
        "signal": "http_5xx_ratio",
        "operator": "less_than",
        "threshold": 0.01,
        "observation_window_seconds": 60,
        "required_successful_samples": 3,
    }
    verification_run_id = verification_body["verification_run"]["id"]

    duplicate_verification = await control_client.post(
        f"/api/v1/operation-executions/{execution_id}/verification-runs",
        json={"expected_incident_version": 999},
    )
    assert duplicate_verification.status_code == 200
    assert duplicate_verification.json() == {**verification_body, "created": False}

    sample = await control_client.post(
        f"/api/v1/verification-runs/{verification_run_id}/samples",
        json={"idempotency_key": "api-verification-sample-001"},
    )
    assert sample.status_code == 201
    sample_body = sample.json()
    assert sample_body["created"] is True
    assert sample_body["probe_performed"] is True
    assert sample_body["sample"]["status"] == "passed"
    assert sample_body["sample"]["observed_value"] == 0.0
    assert sample_body["verification_run"]["status"] == "running"
    assert sample_body["verification_run"]["successful_sample_count"] == 1

    duplicate_sample = await control_client.post(
        f"/api/v1/verification-runs/{verification_run_id}/samples",
        json={"idempotency_key": "api-verification-sample-001"},
    )
    assert duplicate_sample.status_code == 200
    assert duplicate_sample.json() == {
        **sample_body,
        "created": False,
        "probe_performed": False,
    }
    forged_sample = await control_client.post(
        f"/api/v1/verification-runs/{verification_run_id}/samples",
        json={
            "idempotency_key": "api-verification-forged-001",
            "observed_value": 0.0,
        },
    )
    assert forged_sample.status_code == 422
    assert forged_sample.json()["error_code"] == "request_validation_error"
    stored_verification = await control_client.get(
        f"/api/v1/verification-runs/{verification_run_id}"
    )
    assert stored_verification.status_code == 200
    assert stored_verification.json() == sample_body["verification_run"]

    workroom = await control_client.get(
        f"/api/v1/incidents/{created.json()['incident']['id']}/workroom"
    )
    assert workroom.status_code == 200
    workroom_body = workroom.json()
    assert workroom_body["incident_version"] == 5
    assert workroom_body["investigation"]["id"] == run_id
    assert workroom_body["investigation"]["status"] == "succeeded"
    assert workroom_body["evidence_board"]["evidence_count"] == 3
    assert workroom_body["evidence_board"]["specialist_report_count"] == 3
    assert workroom_body["evidence_board"]["primary_hypothesis"]["status"] == "supported"
    assert workroom_body["action_plan"] == {
        "id": plan_id,
        "version": 1,
        "status": "approved",
        "summary": plan_body["action_plan"]["summary"],
        "risk_level": "high",
        "requires_human_approval": True,
        "step_count": 1,
        "created_at": plan_body["action_plan"]["created_at"],
    }
    assert workroom_body["approval_request"]["id"] == request_id
    assert workroom_body["approval_request"]["status"] == "approved"
    assert workroom_body["operation_execution"]["id"] == execution_id
    assert workroom_body["operation_execution"]["status"] == "succeeded"
    assert workroom_body["verification_run"]["id"] == verification_run_id
    assert workroom_body["verification_run"]["status"] == "running"
    assert workroom_body["verification_run"]["successful_sample_count"] == 1
    assert workroom_body["verification_run"]["sample_count"] == 1


async def test_start_investigation_rejects_unknown_mode(
    control_client: httpx2.AsyncClient,
) -> None:
    created = await control_client.post(
        "/api/v1/alerts",
        json=alert_payload("alert-invalid-mode"),
    )
    incident_id = created.json()["incident"]["id"]

    response = await control_client.post(
        f"/api/v1/incidents/{incident_id}/actions/start-investigation",
        json={
            "expected_version": 1,
            "actor_id": "engineer-001",
            "reason": "非法模式",
            "mode": "swarm_everything",
        },
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "request_validation_error"


async def test_missing_investigation_run_returns_structured_404(
    control_client: httpx2.AsyncClient,
) -> None:
    run_id = uuid4()

    response = await control_client.get(f"/api/v1/investigations/{run_id}")

    assert response.status_code == 404
    assert response.json() == {
        "error_code": "investigation_run_not_found",
        "message": f"调查运行不存在：{run_id}",
        "details": {"run_id": str(run_id)},
    }

    for method, path in (
        ("POST", f"/api/v1/investigations/{run_id}/action-plans"),
        ("GET", f"/api/v1/investigations/{run_id}/action-plans/current"),
    ):
        plan_response = await control_client.request(method, path)
        assert plan_response.status_code == 404
        assert plan_response.json()["error_code"] == "investigation_run_not_found"

    plan_id = uuid4()
    missing_plan = await control_client.post(
        f"/api/v1/action-plans/{plan_id}/approval-requests",
        json={
            "expected_incident_version": 1,
            "actor_id": "engineer-001",
            "reason": "目标计划不存在",
        },
    )
    assert missing_plan.status_code == 404
    assert missing_plan.json()["error_code"] == "approval_action_plan_not_found"

    request_id = uuid4()
    missing_request = await control_client.get(f"/api/v1/approval-requests/{request_id}")
    assert missing_request.status_code == 404
    assert missing_request.json()["error_code"] == "approval_request_not_found"

    for action in ("approve", "reject"):
        missing_decision = await control_client.post(
            f"/api/v1/approval-requests/{request_id}/actions/{action}",
            json={
                "expected_incident_version": 1,
                "actor_id": "engineer-001",
                "reason": "目标审批请求不存在",
            },
        )
        assert missing_decision.status_code == 404
        assert missing_decision.json()["error_code"] == "approval_request_not_found"

    step_id = uuid4()
    missing_execution = await control_client.post(
        f"/api/v1/action-steps/{step_id}/executions",
        json={
            "idempotency_key": "missing-execution-001",
            "expected_incident_version": 1,
        },
    )
    assert missing_execution.status_code == 404
    assert missing_execution.json()["error_code"] == "operation_step_not_found"
    execution_id = uuid4()
    missing_execution_record = await control_client.get(
        f"/api/v1/operation-executions/{execution_id}"
    )
    assert missing_execution_record.status_code == 404
    assert missing_execution_record.json()["error_code"] == "operation_execution_not_found"
    missing_verification = await control_client.post(
        f"/api/v1/operation-executions/{execution_id}/verification-runs",
        json={"expected_incident_version": 1},
    )
    assert missing_verification.status_code == 404
    assert missing_verification.json()["error_code"] == "operation_execution_not_found"
    verification_run_id = uuid4()
    missing_verification_record = await control_client.get(
        f"/api/v1/verification-runs/{verification_run_id}"
    )
    assert missing_verification_record.status_code == 404
    assert missing_verification_record.json()["error_code"] == "verification_run_not_found"
    missing_sample = await control_client.post(
        f"/api/v1/verification-runs/{verification_run_id}/samples",
        json={"idempotency_key": "missing-verification-sample-001"},
    )
    assert missing_sample.status_code == 404
    assert missing_sample.json()["error_code"] == "verification_run_not_found"
    missing_stream = await control_client.get(f"/api/v1/incidents/{uuid4()}/stream")
    assert missing_stream.status_code == 404
    assert missing_stream.json()["error_code"] == "incident_not_found"
    invalid_cursor = await control_client.get(
        f"/api/v1/incidents/{uuid4()}/stream",
        headers={"Last-Event-ID": "not-a-number"},
    )
    assert invalid_cursor.status_code == 422
    assert invalid_cursor.json()["error_code"] == "request_validation_error"


async def test_stale_version_and_illegal_transition_return_distinct_409_errors(
    control_client: httpx2.AsyncClient,
) -> None:
    created = await control_client.post(
        "/api/v1/alerts",
        json=alert_payload("alert-001"),
    )
    incident_id = created.json()["incident"]["id"]
    action_url = f"/api/v1/incidents/{incident_id}/actions/start-investigation"
    request_body = {
        "expected_version": 1,
        "actor_id": "engineer-001",
        "reason": "开始调查",
    }

    await control_client.post(action_url, json=request_body)
    stale = await control_client.post(action_url, json=request_body)
    illegal = await control_client.post(
        action_url,
        json={**request_body, "expected_version": 2},
    )

    assert stale.status_code == 409
    assert stale.json()["error_code"] == "incident_version_conflict"
    assert stale.json()["details"] == {"expected_version": 1, "actual_version": 2}
    assert illegal.status_code == 409
    assert illegal.json()["error_code"] == "invalid_incident_transition"
    assert illegal.json()["details"]["status"] == "investigating"


async def test_event_list_supports_forward_cursor_replay(
    control_client: httpx2.AsyncClient,
) -> None:
    created = await control_client.post(
        "/api/v1/alerts",
        json=alert_payload("alert-001"),
    )
    incident_id = created.json()["incident"]["id"]
    await control_client.post(
        f"/api/v1/incidents/{incident_id}/actions/start-investigation",
        json={
            "expected_version": 1,
            "actor_id": "engineer-001",
            "reason": "开始调查",
        },
    )

    first_page = await control_client.get(
        f"/api/v1/incidents/{incident_id}/events",
        params={"limit": 1},
    )
    first_cursor = first_page.json()["next_after_cursor"]
    second_page = await control_client.get(
        f"/api/v1/incidents/{incident_id}/events",
        params={"after_cursor": first_cursor, "limit": 1},
    )

    assert first_page.json()["items"][0]["event_type"] == "incident.created"
    assert isinstance(first_cursor, int)
    assert second_page.json()["items"][0]["event_type"] == "incident.status_changed"
    assert second_page.json()["items"][0]["actor_id"] == "engineer-001"
    assert second_page.json()["items"][0]["reason"] == "开始调查"
    assert second_page.json()["next_after_cursor"] is None


async def test_missing_incident_returns_structured_404(
    control_client: httpx2.AsyncClient,
) -> None:
    incident_id = uuid4()

    response = await control_client.get(f"/api/v1/incidents/{incident_id}")
    workroom = await control_client.get(f"/api/v1/incidents/{incident_id}/workroom")

    assert response.status_code == 404
    expected_error = {
        "error_code": "incident_not_found",
        "message": f"Incident 不存在：{incident_id}",
        "details": {"incident_id": str(incident_id)},
    }
    assert response.json() == expected_error
    assert workroom.status_code == 404
    assert workroom.json() == expected_error


async def test_request_validation_rejects_unknown_fields_and_naive_time(
    control_client: httpx2.AsyncClient,
) -> None:
    unknown_field_payload = alert_payload("alert-001")
    unknown_field_payload["ingested_at"] = "2026-01-01T08:00:00Z"
    naive_time_payload = alert_payload("alert-002")
    naive_time_payload["occurred_at"] = "2026-01-01T07:59:59"

    unknown_field = await control_client.post(
        "/api/v1/alerts",
        json=unknown_field_payload,
    )
    naive_time = await control_client.post(
        "/api/v1/alerts",
        json=naive_time_payload,
    )

    assert unknown_field.status_code == 422
    assert unknown_field.json()["error_code"] == "request_validation_error"
    assert unknown_field.json()["details"]["issues"][0]["location"] == "body.ingested_at"
    assert naive_time.status_code == 422
    assert naive_time.json()["error_code"] == "request_validation_error"


async def test_domain_payload_limit_maps_to_structured_422(
    control_client: httpx2.AsyncClient,
) -> None:
    payload = alert_payload("alert-001")
    payload["payload"] = {"content": "故" * (64 * 1024)}

    response = await control_client.post("/api/v1/alerts", json=payload)

    assert response.status_code == 422
    assert response.json()["error_code"] == "incident_validation_error"
