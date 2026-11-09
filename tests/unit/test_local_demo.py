"""演示配置、脚本输出和拒绝路径均不依赖 Docker 或外部模型。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import httpx2
import pytest
from dotenv import dotenv_values
from pydantic import TypeAdapter

from opsswarm.change_records.models import ChangeSpec
from opsswarm.control_plane.app import create_app
from opsswarm.control_plane.schemas import AlertRequest, StartInvestigationRequest
from opsswarm.identity.settings import OperatorAuthSettings
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy
from opsswarm.investigation.candidate.contracts import EvaluateCandidateCommand
from opsswarm.investigation.coordination.contracts import CoordinationProposal
from opsswarm.investigation.human_prepare.contracts import PrepareCommand
from opsswarm.investigation.human_review.contracts import ReassessCommand
from opsswarm.investigation.human_wait.contracts import SupplementWaitCommand
from opsswarm.investigation.models import SpecialistReport
from opsswarm.investigation.review.contracts import HypothesisReviewProposal
from opsswarm.investigation.synthesis.contracts import OpenHypothesisProposal
from opsswarm.observability import TracingRuntime
from scripts.local_demo.__main__ import execute
from scripts.local_demo.client import DemoClient, DemoError
from scripts.local_demo.config import (
    MODEL,
    PROJECT,
    ROLES,
    directory,
    model_budget,
    prepare,
    read_config,
)
from scripts.local_demo.disposition import execute_and_converge
from scripts.local_demo.environment import ComposeControl, preflight
from scripts.local_demo.fixture import completion
from scripts.local_demo.investigation import Investigation
from scripts.local_demo.model_server import app


@pytest.fixture
def demo_dir():
    root = directory()
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="unit-", dir=root) as name:
        path = directory(name)
        # 临时目录的自动清理也必须限制在项目的演示目录中。
        assert path.is_relative_to(root)
        yield path


def test_prepare_creates_independent_valid_roles_and_reuses_config(demo_dir):
    env = prepare(demo_dir)
    config = read_config(demo_dir)
    original = env.read_bytes()
    assert prepare(demo_dir) == env and env.read_bytes() == original
    auth = OperatorAuthSettings(
        mode="bearer",
        credentials=json.loads(dotenv_values(env)["OPSSWARM_OPERATOR_AUTH_CREDENTIALS"]),
    )
    assert {item.role.value for item in auth.credentials} == set(ROLES)
    assert len(set(config["tokens"].values())) == len(ROLES)
    for item in auth.credentials:
        credential_id, secret = config["tokens"][item.role].split(".", 1)
        assert credential_id == item.credential_id
        assert item.secret_sha256 == hashlib.sha256(secret.encode()).hexdigest()
        assert secret not in env.read_text(encoding="utf-8")
    assert ModelBudgetPolicy.model_validate(model_budget()).profile.model_name == MODEL


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True], ids=["new-config", "legacy-config"])
async def test_generated_tokens_are_accepted_by_actual_control_api(demo_dir, legacy):
    env = prepare(demo_dir)
    original_env = env.read_bytes()
    path = demo_dir / "config.json"
    if legacy:
        old = json.loads(path.read_text(encoding="utf-8"))
        old["tokens"] = {role: token.split(".", 1)[1] for role, token in old["tokens"].items()}
        path.write_text(json.dumps(old), encoding="utf-8")
    original_config = path.read_bytes()
    config = read_config(demo_dir)
    auth = OperatorAuthSettings(
        mode="bearer",
        credentials=json.loads(dotenv_values(env)["OPSSWARM_OPERATOR_AUTH_CREDENTIALS"]),
    )
    api = create_app(operator_auth_settings=auth, tracing_runtime=TracingRuntime())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=api), base_url="http://testserver"
    ) as client:
        endpoint = "/api/v1/operators/me"
        assert (await client.get(endpoint)).status_code == 401
        for role, token in config["tokens"].items():
            response = await client.get(endpoint, headers={"Authorization": f"Bearer {token}"})
            assert response.status_code == 200
            assert response.json()["role"] == role
    assert prepare(demo_dir) == env
    assert path.read_bytes() == original_config and env.read_bytes() == original_env


def test_start_sends_current_api_contract_and_reads_persisted_handoff(monkeypatch):
    case_id, incident, run, handoff = (str(UUID(int=i)) for i in range(1, 5))
    base = f"/api/v1/investigations/{run}"
    start_path = f"/api/v1/incidents/{incident}/actions/start-investigation"
    occurred_at = "2026-11-09T00:00:00+00:00"
    starts = []

    def respond(request):
        path, method = request.url.path, request.method
        if not path.startswith("/api/"):
            assert "authorization" not in request.headers
        if path == "/__admin/configuration/database-pool":
            return httpx2.Response(200, json={"value": 50})
        if path == "/__admin/simulated-release":
            return httpx2.Response(200, json={"version": "stable", "revision": 1})
        if path == "/orders/local-demo":
            return httpx2.Response(500)
        if path == "/__admin/changes" and method == "POST":
            TypeAdapter(ChangeSpec).validate_json(request.content)
            return httpx2.Response(201, json={"record": {"occurred_at": occurred_at}})
        if path == "/api/v1/alerts" and method == "POST":
            alert = AlertRequest.model_validate_json(request.content)
            assert alert.external_event_id == case_id
            assert alert.occurred_at.isoformat() == occurred_at
            return httpx2.Response(201, json={"incident": {"id": incident}})
        if path == start_path and method == "POST":
            starts.append(StartInvestigationRequest.model_validate_json(request.content))
            return httpx2.Response(200, json={"investigation": {"id": run}})
        if path == base:
            return httpx2.Response(200, json={"status": "await_human"})
        if path == base + "/human-wait":
            return httpx2.Response(
                200,
                json={
                    "handoff_id": handoff,
                    "state_version": 1,
                    "status": "waiting",
                },
            )
        if path == base + "/collaboration":
            return httpx2.Response(
                200,
                json={
                    "rounds": [
                        {
                            "round_number": 1,
                            "status": "completed",
                            "tasks": [
                                {"assignment": {"role": role}, "status": "succeeded"}
                                for role in ("log", "metric", "change")
                            ],
                        }
                    ]
                },
            )
        if path == base + "/call-budget":
            return httpx2.Response(200, json={"reserved_model_calls": 9, "deadline": occurred_at})
        raise AssertionError(f"意外请求 {method} {path}")

    monkeypatch.setattr("scripts.local_demo.investigation.time.sleep", lambda _: None)
    client = DemoClient(dict.fromkeys(ROLES, "test-only"), transport=httpx2.MockTransport(respond))
    emit = MagicMock()
    try:
        Investigation(client, case_id, emit).start()
    finally:
        client.close()
    assert len(starts) == 1
    assert starts[0].workflow_version == "v3" and starts[0].data_source_mode == "live"
    assert emit.call_args.kwargs["handoff_id"] == handoff
    assert emit.call_args.kwargs["completed_rounds"] == [1]


@pytest.mark.parametrize("persisted", [True, False], ids=["read-back", "missing-event"])
def test_human_continuation_uses_persisted_wait_and_submitted_job_ids(persisted):
    case, incident, run, handoff, event_id, prep, review, route, candidate = (
        str(UUID(int=i)) for i in range(1, 10)
    )
    base = f"/api/v1/investigations/{run}"
    wait = base + "/human-wait"
    preparations = wait + "/preparations"
    reassessments = f"{preparations}/{prep}/reassessments"
    evaluations = base + "/candidate-evaluations"
    nav = f"/api/v1/incidents/{incident}/collaboration-navigation"
    event = {"id": event_id, "state_version": 2, "action": "supplement"}
    writes = []

    def respond(request):
        path = request.url.path
        if request.method == "POST":
            writes.append(path)
            if path == wait + "/supplements":
                command = SupplementWaitCommand.model_validate_json(request.content)
                assert command.expected_wait_version == 1 and str(command.handoff_id) == handoff
                return httpx2.Response(201, json={"applied": True, "event": event})
            if path == preparations:
                command = PrepareCommand.model_validate_json(request.content)
                assert command.expected_wait_version == 2 and command.source_sha256 == "a" * 64
                return httpx2.Response(201, json={"job": {"id": prep}})
            if path == reassessments:
                command = ReassessCommand.model_validate_json(request.content)
                assert command.expected_wait_version == 2 and command.preparation_sha256 == "b" * 64
                return httpx2.Response(201, json={"job": {"id": review}})
            if path == evaluations:
                command = EvaluateCandidateCommand.model_validate_json(request.content)
                assert command.expected_wait_version == 2 and str(command.source_id) == route
                assert command.source_sha256 == "c" * 64
                return httpx2.Response(201, json={"job": {"id": candidate}})
        if path == wait:
            saved = bool(writes) and persisted
            return httpx2.Response(
                200,
                json={
                    "handoff_id": handoff,
                    "source_sha256": "a" * 64,
                    "state_version": 2 if saved else 1,
                    "status": "waiting",
                    "events": [event] if saved else [],
                },
            )
        if path == f"{preparations}/{prep}":
            return httpx2.Response(
                200,
                json={
                    "status": "succeeded",
                    "availability": "ready",
                    "wait_version": 2,
                    "artifact_sha256": "b" * 64,
                },
            )
        if path == f"{reassessments}/{review}":
            return httpx2.Response(200, json={"status": "succeeded"})
        if path == f"{reassessments}/{review}/route":
            return httpx2.Response(
                200,
                json={
                    "availability": "current",
                    "record": {
                        "id": route,
                        "wait_version": 2,
                        "decision": {"route": "candidate_evaluation"},
                    },
                },
            )
        if path == nav:
            return httpx2.Response(
                200, json={"review_job_id": review, "review_route_sha256": "c" * 64}
            )
        if path == f"{evaluations}/{candidate}":
            return httpx2.Response(
                200,
                json={
                    "status": "succeeded",
                    "availability": "current",
                    "wait_version": 2,
                    "result": {"content": {"disposition": "planning_review"}},
                },
            )
        raise AssertionError(f"意外请求 {request.method} {path}")

    client = DemoClient(dict.fromkeys(ROLES, "test-only"), transport=httpx2.MockTransport(respond))
    emit = MagicMock()
    flow = Investigation(client, case, emit)
    flow.base, flow.incident = base, f"/api/v1/incidents/{incident}"
    try:
        if not persisted:
            with pytest.raises(DemoError, match="supplement_not_persisted"):
                flow.continue_human()
            assert writes == [wait + "/supplements"]
            emit.assert_not_called()
            return
        assert flow.continue_human() == f"{evaluations}/{candidate}"
    finally:
        client.close()
    assert writes == [wait + "/supplements", preparations, reassessments, evaluations]
    emit.assert_called_once_with(
        "human_continued",
        supplement_id=event_id,
        wait_version=2,
        preparation_id=prep,
        review_job_id=review,
        review_route_id=route,
        candidate_id=candidate,
    )


def test_prepare_refuses_expired_or_partial_configuration(demo_dir):
    prepare(demo_dir)
    data = read_config(demo_dir)
    data["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    (demo_dir / "config.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="过期"):
        prepare(demo_dir)
    (demo_dir / "config.json").unlink()
    with pytest.raises(ValueError, match="未完成"):
        prepare(demo_dir)


def test_prepare_refuses_missing_environment_instead_of_reusing_partial_configuration(demo_dir):
    prepare(demo_dir)
    (demo_dir / "demo.env").unlink()
    with pytest.raises(ValueError, match="环境文件缺失"):
        prepare(demo_dir)


def test_prepare_rejects_paths_outside_owned_demo_directory():
    with pytest.raises(ValueError, match="演示文件"):
        directory("src")


def request(data, tools, *, detail=None):
    messages = [
        dict(role="system", content="脚本演示"),
        dict(role="user", content=json.dumps(data)),
    ]
    if detail is not None:
        messages.append(dict(role="tool", content=json.dumps(detail)))
    return dict(
        model=MODEL, messages=messages, tools=[dict(function=dict(name=name)) for name in tools]
    )


def answer(payload):
    result = completion(payload)
    call = result["choices"][0]["message"]["tool_calls"][0]["function"]
    return call["name"], json.loads(call["arguments"])


def test_fixture_plans_actual_mcp_roles_and_reads_only_catalogued_evidence():
    _, proposal = answer(request(dict(policy={}, round_number=1), ["CoordinationProposal"]))
    parsed = CoordinationProposal.model_validate(proposal)
    assert {task.role.value for task in parsed.tasks} == {"log", "metric", "change"}
    data = dict(
        specialist="log",
        evidence_catalog=dict(entries=[dict(evidence_id="actual-log")], snapshot_sha256="a" * 64),
    )
    name, read = answer(request(data, ["read_evidence", "SpecialistReport"]))
    assert name == "read_evidence" and read["evidence_id"] == "actual-log"
    name, report = answer(
        request(
            data,
            ["read_evidence", "SpecialistReport"],
            detail=dict(evidence=dict(evidence_id="actual-log")),
        )
    )
    assert name == "SpecialistReport"
    assert SpecialistReport.model_validate(report).evidence_ids == ("actual-log",)


def test_fixture_uses_change_support_and_does_not_invent_missing_source():
    data = dict(
        working_summaries=[],
        evidence=[dict(kind="log", evidence_id="log"), dict(kind="change", evidence_id="change")],
    )
    _, proposal = answer(request(data, ["OpenHypothesisProposal"]))
    assert OpenHypothesisProposal.model_validate(proposal).supporting_evidence_ids == ("change",)
    data["evidence"] = []
    with pytest.raises(ValueError, match="缺少实际"):
        completion(request(data, ["OpenHypothesisProposal"]))
    with pytest.raises(ValueError, match="演示模型"):
        completion({**request(data, ["OpenHypothesisProposal"]), "model": "real-model"})


@pytest.mark.parametrize("review", [False, True])
def test_reflow_fixture_preserves_input_ids_and_requires_human_decision(review):
    data = dict(professionals=[dict(evidence=dict(evidence=[dict(evidence_id="new-change")]))])
    if review:
        data["synthesis_result"] = {}
    _, result = answer(
        request(data, ["HypothesisReviewProposal" if review else "OpenHypothesisProposal"])
    )
    if review:
        assert HypothesisReviewProposal.model_validate(result).accepted_supporting_evidence_ids == (
            "new-change",
        )
    else:
        assert OpenHypothesisProposal.model_validate(result).supporting_evidence_ids == (
            "new-change",
        )


@pytest.mark.asyncio
async def test_fixture_http_protocol_health_and_invalid_request():
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        assert (await client.get("/health")).json()["scripted"] is True
        assert (await client.post("/v1/chat/completions", json={})).status_code == 422
        assert (await client.post("/v1/chat/completions", json=[])).status_code == 422
        valid = request(dict(policy={}, round_number=1), ["CoordinationProposal"])
        assert (await client.post("/v1/chat/completions", json=valid)).status_code == 200


def test_write_timeout_is_not_retried_and_redirects_are_not_followed():
    calls = []

    def timeout(request):
        calls.append(request.method)
        raise httpx2.ReadTimeout("unknown", request=request)

    client = DemoClient(dict(engineer="private"), transport=httpx2.MockTransport(timeout))
    with pytest.raises(DemoError, match="transport_result_unknown"):
        client.call("/command", {"idempotency_key": "stable"})
    assert calls == ["POST"]
    with pytest.raises(DemoError, match="outside_local_demo"):
        client.call("/command", origin="https://example.com")
    client.close()
    client = DemoClient(
        dict(engineer="private"),
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(302, headers={"Location": "https://example.com"})
        ),
    )
    with pytest.raises(DemoError, match="http_302"):
        client.call("/redirect")
    client.close()


def test_clock_block_stops_before_api_and_report_never_contains_credentials(demo_dir):
    prepare(demo_dir)
    client = MagicMock()
    client.call.side_effect = AssertionError("时钟失败前不能访问业务 API")
    control = SimpleNamespace(clock=lambda: dict(negative_steps=1, steps_over_100ms=1))
    with pytest.raises(DemoError, match="unstable_container_clock"):
        preflight(client, control)
    code, report = execute(demo_dir, scenario="recovery", client=client, control=control)
    data = json.loads(report.read_text(encoding="utf-8"))
    assert code == 2 and data["status"] == "blocked_environment"
    assert data["error"]["clock"]["negative_steps"] == 1
    client.call.assert_not_called()
    assert not any(
        token in report.read_text(encoding="utf-8")
        for token in read_config(demo_dir)["tokens"].values()
    )


def test_failure_scenario_restores_sampling_when_preparation_result_is_unknown():
    control = MagicMock()
    client = MagicMock()
    client.call.side_effect = [
        dict(version="2026.01.01-rc1", revision=2),
        dict(record=dict(id="approval", content_sha256="a" * 64)),
    ]
    flow = SimpleNamespace(
        client=client,
        version=lambda: dict(expected_incident_version=1),
        post=MagicMock(side_effect=DemoError("prepare", "transport_result_unknown")),
    )
    with pytest.raises(DemoError, match="transport_result_unknown"):
        execute_and_converge(flow, "/conditions/1", control, failure=True)
    assert [call.args[0] for call in control.sampling.call_args_list] == [False, True]
    assert flow.post.call_count == 1


def test_compose_commands_are_bound_to_project_and_env_path(demo_dir):
    args = ComposeControl(demo_dir).command("stop", "recovery-sampling-worker")
    assert args[args.index("-p") + 1] == PROJECT
    assert args[args.index("--env-file") + 1] == str(demo_dir / "demo.env")
    assert args[-2:] == ["stop", "recovery-sampling-worker"]


def test_unknown_stop_result_still_attempts_sampling_restore():
    control = MagicMock()
    control.sampling.side_effect = [DemoError("compose", "local_demo_command_failed"), None]
    client = MagicMock()
    flow = SimpleNamespace(client=client)
    with pytest.raises(DemoError, match="local_demo_command_failed"):
        execute_and_converge(flow, "/conditions/1", control, failure=True)
    assert [call.args[0] for call in control.sampling.call_args_list] == [False, True]
