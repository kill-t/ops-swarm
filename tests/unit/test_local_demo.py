"""演示配置、脚本输出和拒绝路径均不依赖 Docker 或外部模型。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import httpx2
import pytest
from dotenv import dotenv_values
from pydantic import TypeAdapter

from opsswarm.change_records.models import ChangeSpec
from opsswarm.control_plane.app import create_app
from opsswarm.control_plane.schemas import AlertRequest, StartInvestigationRequest
from opsswarm.identity.settings import OperatorAuthSettings
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.coordination.contracts import CoordinationProposal
from opsswarm.investigation.human_wait.contracts import SupplementWaitCommand
from opsswarm.investigation.models import SpecialistReport
from opsswarm.investigation.reinvestigation_review.contracts import ReflowReviewResult
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
from scripts.local_demo.disposition import approve_and_verify, execute_and_converge
from scripts.local_demo.environment import ComposeControl, preflight
from scripts.local_demo.fixture import completion
from scripts.local_demo.investigation import Investigation
from scripts.local_demo.model_server import app
from scripts.local_demo.reflow import reinvestigate


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


def test_human_continuation_stops_if_supplement_is_missing_from_wait():
    handoff = str(UUID(int=2))
    wait = {
        "handoff_id": handoff,
        "source_sha256": "a" * 64,
        "state_version": 1,
        "status": "waiting",
        "events": [],
    }
    client = MagicMock()
    client.call.side_effect = [wait, {"applied": True, "event": {"id": str(UUID(int=3))}}, wait]
    emit = MagicMock()
    flow = Investigation(client, str(UUID(int=1)), emit)
    flow.base = "/run"
    with pytest.raises(DemoError, match="supplement_not_persisted"):
        flow.continue_human()
    assert [call.args[0] for call in client.call.call_args_list] == [
        "/run/human-wait",
        "/run/human-wait/supplements",
        "/run/human-wait",
    ]
    command = SupplementWaitCommand.model_validate(client.call.call_args_list[1].args[1])
    assert command.expected_wait_version == 1 and str(command.handoff_id) == handoff
    emit.assert_not_called()


def test_restart_waits_for_readiness_and_rejects_changed_budget(monkeypatch):
    before = {"wait": {"status": "waiting"}, "rounds": [], "budget": {"calls": ["original"]}}
    responses = iter(
        [
            httpx2.Response(502),
            httpx2.Response(200, json={"role": "engineer"}),
            httpx2.Response(200, json={"status": "await_human"}),
            httpx2.Response(200, json=before["wait"]),
            httpx2.Response(200, json={"rounds": before["rounds"]}),
            httpx2.Response(200, json={"calls": []}),
        ]
    )
    reads = []

    def respond(request):
        assert request.method == "GET"
        reads.append(request.url.path)
        return next(responses)

    monkeypatch.setattr("scripts.local_demo.client.time.sleep", lambda _: None)
    control = MagicMock()
    client = DemoClient(dict(engineer="test-only"), transport=httpx2.MockTransport(respond))
    emit = MagicMock()
    flow = Investigation(client, str(UUID(int=1)), emit)
    flow.base = "/run"
    try:
        with pytest.raises(DemoError, match="persisted_handoff_changed"):
            flow.recover_handoff(control, before)
    finally:
        client.close()
    assert reads[:2] == ["/api/v1/operators/me"] * 2
    control.restart_handoff_services.assert_called_once_with()
    emit.assert_not_called()


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


@pytest.mark.parametrize("date", [None, "invalid-date", "Mon, 09 Nov 2026 06:00:00 GMT"])
def test_order_failure_uses_valid_server_response_time(date):
    def respond(request):
        assert request.method == "GET" and request.url.path == "/orders/local-demo"
        assert "authorization" not in request.headers
        return httpx2.Response(500, headers={"Date": date} if date else {})

    client = DemoClient({}, transport=httpx2.MockTransport(respond))
    try:
        if date is None or date == "invalid-date":
            with pytest.raises(DemoError, match="invalid_order_response_time"):
                client.observe_order_failure()
        else:
            assert client.observe_order_failure() == datetime(2026, 11, 9, 6, tzinfo=UTC)
    finally:
        client.close()


def test_conditions_stop_when_order_response_stays_older_than_approval(monkeypatch):
    client = MagicMock()
    client.call.side_effect = [
        {"wait_version": 2, "result": {"content_sha256": "candidate-hash"}},
        {"record": {"content_sha256": "plan-hash"}},
        {"record": {"id": "request", "plan_content_sha256": "plan-hash"}},
        {
            "record": {
                "id": "approval",
                "outcome": "approved",
                "content_sha256": "approval-hash",
                "created_at": "2026-11-09T06:00:00.100000+00:00",
            }
        },
        {"catalog": [{"text": "确认当前演练版本与发布依据一致"}]},
        {"version": "2026.01.01-rc1", "revision": 2},
    ]
    client.observe_order_failure.return_value = datetime(2026, 11, 9, 6, tzinfo=UTC)
    flow = SimpleNamespace(
        client=client,
        post=MagicMock(),
        version=lambda: {"expected_incident_version": 2},
    )
    monkeypatch.setattr("scripts.local_demo.disposition.time.sleep", lambda _: None)
    with pytest.raises(DemoError, match="observation_predates_approval"):
        approve_and_verify(flow, "/candidate")
    assert client.observe_order_failure.call_count == 3
    assert all("/execution-conditions" not in call.args[0] for call in flow.post.call_args_list)


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


@pytest.mark.parametrize(
    ("body", "details"),
    [
        (
            {"error_code": "release_execution_source_stale", "message": "内部详情"},
            {"api_error_code": "release_execution_source_stale"},
        ),
        ({"error_code": "token: secret"}, {}),
    ],
)
def test_failed_request_keeps_only_the_api_machine_code(body, details):
    client = DemoClient(
        {"engineer": "fixture"},
        transport=httpx2.MockTransport(lambda _: httpx2.Response(409, json=body)),
    )
    try:
        with pytest.raises(DemoError, match="http_409") as caught:
            client.call("/execution-job")
        assert caught.value.details == details
    finally:
        client.close()


def test_expired_release_preparation_is_recorded_without_creating_execution_job():
    client = MagicMock()
    client.call.side_effect = [
        dict(version="2026.01.01-rc1", revision=2),
        dict(record=dict(id="approval", content_sha256="a" * 64)),
    ]
    record = dict(
        id="request",
        created_at="2026-11-09T06:00:00Z",
        expires_at="2026-11-09T06:00:30Z",
    )
    flow = SimpleNamespace(
        client=client,
        version=lambda: dict(expected_incident_version=1),
        post=MagicMock(return_value=dict(request=dict(record=record, availability="expired"))),
        emit=MagicMock(),
    )
    with pytest.raises(DemoError, match="release_preparation_stale"):
        execute_and_converge(flow, "/conditions/1", MagicMock(), failure=False)
    flow.emit.assert_called_once_with(
        "release_prepared",
        request_id=record["id"],
        availability="expired",
        created_at=record["created_at"],
        expires_at=record["expires_at"],
    )
    assert flow.post.call_count == 1


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


@pytest.mark.parametrize(
    ("mismatch", "error_code"),
    [
        (None, None),
        ("admission", "admission_audit_mismatch"),
        ("evidence", "new_change_not_in_professional_report"),
        ("review", "new_change_not_in_routed_review"),
        ("revision", "release_changed_during_reflow"),
    ],
)
def test_reflow_verifies_durable_admission_evidence_and_single_release(
    monkeypatch, mismatch, error_code
):
    case_id, task_id, change_id, admission_id = (str(uuid4()) for _ in range(4))
    evidence_id = "change-" + change_id
    window = {"window_start": "2026-11-13T13:00:02Z", "window_end": "2026-11-13T13:00:10Z"}
    review = ReflowReviewResult(
        investigation_run_id=UUID(case_id),
        job_id=UUID(case_id),
        source_call_id=uuid4(),
        source_sha256="a" * 64,
        proposal=HypothesisReviewProposal(
            support_is_sufficient=True,
            rationale="复核配置变更",
            accepted_supporting_evidence_ids=(evidence_id,),
            suggested_direction="evaluate_for_routing",
        ),
        synthesis_result_sha256="b" * 64,
        created_at=datetime(2026, 11, 13, 13, 0, 11, tzinfo=UTC),
    )
    routed = dict(
        route="await_human",
        dispatch_status="not_dispatched",
        blocking_reason_codes=["live_reflow_manual_takeover"],
        observation_windows=[dict(task_id=task_id, effective_window=dict(window=window))],
        review_result_sha256="c" * 64 if mismatch == "review" else content_hash(review),
        created_at="2026-11-13T13:00:12Z",
    )
    task = dict(
        task_id=task_id,
        status="succeeded",
        effective_window=dict(window=window),
        evidence=dict(
            data_source_mode="live", role="change", evidence=[dict(evidence_id=evidence_id)]
        ),
        result=dict(
            report=dict(evidence_ids=["previous-change" if mismatch == "evidence" else evidence_id])
        ),
    )
    audit = [
        dict(outcome="applied", request_id=admission_id, error_code=None),
        dict(outcome="replayed", request_id=admission_id, error_code=None),
        dict(
            outcome="error",
            request_id=None,
            error_code=(
                "reinvestigation_basis_changed"
                if mismatch == "admission"
                else "reinvestigation_already_exists"
            ),
        ),
    ]
    calls = [dict(status="succeeded", id="original")]
    budget = dict(deadline="2026-11-13T13:30:00Z", policy={}, model_budget={}, calls=calls)
    incident = "/api/v1/incidents/incident"
    base = f"/api/v1/investigations/{case_id}"
    reads = {
        base + "/reinvestigation-admission": dict(record=dict(id=admission_id)),
        base + "/reinvestigation-admission/commands": dict(items=audit),
        base + "/reinvestigation-routing": dict(record=routed),
        base + "/reinvestigation-execution": dict(tasks=[task]),
        base + "/reinvestigation-review": dict(
            status="succeeded", result=review.model_dump(mode="json")
        ),
        incident: dict(status="investigating"),
    }
    admission_keys = []
    pool_writes = []
    release_reads = 0

    def respond(request):
        nonlocal release_reads
        path = request.url.path
        if path == "/__admin/simulated-release":
            release_reads += 1
            return httpx2.Response(
                200,
                json=dict(
                    version="stable", revision=3 + (release_reads == 2 and mismatch == "revision")
                ),
            )
        if path == "/__admin/configuration/database-pool":
            assert request.method == "PUT"
            pool_writes.append(json.loads(request.content)["value"])
            return httpx2.Response(200)
        if path == "/__admin/changes":
            assert request.method == "POST"
            return httpx2.Response(
                201, json=dict(record=dict(record_id=change_id, occurred_at="2026-11-13T13:00:03Z"))
            )
        if request.method == "POST":
            assert path == base + "/reinvestigation-admission"
            admission_keys.append(json.loads(request.content)["idempotency_key"])
            return httpx2.Response((201, 200, 409)[len(admission_keys) - 1])
        if path == base + "/call-budget":
            return httpx2.Response(
                200,
                json={
                    **budget,
                    "calls": calls
                    + ([dict(status="succeeded", id="new")] if admission_keys else []),
                },
            )
        return httpx2.Response(200, json=reads[path])

    monkeypatch.setattr("scripts.local_demo.reflow.time.sleep", lambda _: None)
    events = {}
    client = DemoClient({"engineer": "fixture"}, transport=httpx2.MockTransport(respond))
    flow = Investigation(client, case_id, lambda stage, **values: events.update({stage: values}))
    flow.base, flow.incident = base, incident
    convergence = dict(content_sha256="d" * 64, after_version=3)
    try:
        if error_code:
            with pytest.raises(DemoError, match=error_code):
                reinvestigate(flow, convergence)
            assert pool_writes == [2]
            assert "reinvestigation" not in events
        else:
            reinvestigate(flow, convergence)
            assert pool_writes == [2, 50]
            assert events["reinvestigation_admitted"]["change_record_id"] == change_id
            assert events["reinvestigation"]["evidence_id"] == evidence_id
            assert events["reinvestigation"]["review_result_sha256"] == content_hash(review)
    finally:
        client.close()
    assert admission_keys[0] == admission_keys[1] != admission_keys[2]


def test_compose_commands_are_bound_to_project_and_env_path(demo_dir, monkeypatch):
    control = ComposeControl(demo_dir)
    run = MagicMock()
    monkeypatch.setattr(control, "_run", run)
    control.restart_handoff_services()
    restart = run.call_args.args
    assert restart == (
        "restart",
        "--no-deps",
        "-t",
        "15",
        "control-api",
        "investigation-worker-frozen",
    )
    args = control.command(*restart)
    assert args[args.index("-p") + 1] == PROJECT
    assert args[args.index("--env-file") + 1] == str(demo_dir / "demo.env")
    assert args[-2:] == ["control-api", "investigation-worker-frozen"]


def test_unknown_stop_result_still_attempts_sampling_restore():
    control = MagicMock()
    control.sampling.side_effect = [DemoError("compose", "local_demo_command_failed"), None]
    client = MagicMock()
    flow = SimpleNamespace(client=client)
    with pytest.raises(DemoError, match="local_demo_command_failed"):
        execute_and_converge(flow, "/conditions/1", control, failure=True)
    assert [call.args[0] for call in control.sampling.call_args_list] == [False, True]
