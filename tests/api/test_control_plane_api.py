"""控制面 API 与真实 PostgreSQL 的集成测试。"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx2
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
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

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def fixed_clock() -> datetime:
    return BASE_TIME


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

    assert response.status_code == 404
    assert response.json() == {
        "error_code": "incident_not_found",
        "message": f"Incident 不存在：{incident_id}",
        "details": {"incident_id": str(incident_id)},
    }


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
