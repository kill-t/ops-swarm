"""用假探针和真实业务持久链构造完整复盘，不跳过审批及恢复验证。"""

from datetime import datetime, timedelta

from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
from tests.api.test_control_plane_api import (
    BASE_TIME,
    alert_payload,
    complete_scenario_investigation,
)
from tests.identity_helpers import auth_header


class ExperienceClock:
    def __init__(self) -> None:
        self.value = BASE_TIME

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


async def completed_source_report(client, factory, clock):
    async def post(path, *, role="engineer", body=None):
        response = await client.post(path, json=body, headers=auth_header(role))
        assert response.status_code in (200, 201), response.text
        return response.json()

    incident = (
        await post("/api/v1/alerts", role="alert_source", body=alert_payload("experience-source"))
    )["incident"]
    started = await post(
        f"/api/v1/incidents/{incident['id']}/actions/start-investigation",
        body={"expected_version": 1, "reason": "经验来源测试调查"},
    )
    await complete_scenario_investigation(
        factory,
        run_id=started["investigation"]["id"],
        scenario=RELEASE_500_SCENARIO,
        worker_id="experience-source-worker",
    )
    clock.advance(3)
    plan = (await post(f"/api/v1/investigations/{started['investigation']['id']}/action-plans"))[
        "action_plan"
    ]
    approval = (
        await post(
            f"/api/v1/action-plans/{plan['id']}/approval-requests",
            body={"expected_incident_version": 2, "reason": "提交白名单回滚"},
        )
    )["approval_request"]
    await post(
        f"/api/v1/approval-requests/{approval['id']}/actions/approve",
        role="approver",
        body={"expected_incident_version": 3, "reason": "核对后批准"},
    )
    execution = (
        await post(
            f"/api/v1/action-steps/{plan['steps'][0]['id']}/executions",
            role="executor",
            body={"expected_incident_version": 4, "idempotency_key": "experience-execution"},
        )
    )["operation_execution"]
    verification = (
        await post(
            f"/api/v1/operation-executions/{execution['id']}/verification-runs",
            role="executor",
            body={"expected_incident_version": 5},
        )
    )["verification_run"]
    for index in range(3):
        sampled = await post(
            f"/api/v1/verification-runs/{verification['id']}/samples",
            role="executor",
            body={"idempotency_key": f"experience-sample-{index}"},
        )
        if index != 2:
            clock.advance(30)
    assert sampled["verification_run"]["status"] == "passed"
    await post(
        f"/api/v1/incidents/{incident['id']}/actions/close",
        body={"expected_version": 6, "reason": "完整验证后关闭事故"},
    )
    return (
        await post(
            f"/api/v1/incidents/{incident['id']}/reports",
            body={"expected_incident_version": 7},
        )
    )["report"]
