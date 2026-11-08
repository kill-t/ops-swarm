"""演示配置、脚本输出和拒绝路径均不依赖 Docker 或外部模型。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx2
import pytest
from dotenv import dotenv_values

from opsswarm.identity.settings import OperatorAuthSettings
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy
from opsswarm.investigation.coordination.contracts import CoordinationProposal
from opsswarm.investigation.models import SpecialistReport
from opsswarm.investigation.review.contracts import HypothesisReviewProposal
from opsswarm.investigation.synthesis.contracts import OpenHypothesisProposal
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
        assert (
            item.secret_sha256 == hashlib.sha256(config["tokens"][item.role].encode()).hexdigest()
        )
        assert config["tokens"][item.role] not in env.read_text(encoding="utf-8")
    assert ModelBudgetPolicy.model_validate(model_budget()).profile.model_name == MODEL


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
