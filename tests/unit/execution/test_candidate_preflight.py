"""候选前置检查的严格契约、保守阻断及有界只读观测。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx2
import pytest
from pydantic import ValidationError

from opsswarm.approval.decision.contracts import DecisionContent, DecisionRecord
from opsswarm.execution.candidate.contracts import (
    OBSERVATION_PATHS,
    PreflightCommand,
    PreflightContent,
    PreflightError,
    RuntimeObservation,
)
from opsswarm.execution.candidate.observer import MAX_RESPONSE_BYTES, FaultLabPreflightObserver
from opsswarm.execution.candidate.policy import build_content, validate_step
from opsswarm.execution.settings import ExecutionSettings
from opsswarm.fault_lab.app import create_app
from opsswarm.fault_lab.controller import FaultController
from opsswarm.investigation.models import HypothesisStatus, Recommendation, RootCause
from opsswarm.investigation.scenarios import (
    DATABASE_POOL_SCENARIO_V2,
    REDIS_UNAVAILABLE_SCENARIO_V2,
    RELEASE_500_SCENARIO,
)
from opsswarm.planning import propose_action_plan
from opsswarm.planning.models import ActionType

NOW = datetime(2026, 10, 25, 4, tzinfo=UTC)


def step_for(action=ActionType.ROLLBACK_CONFIGURATION):
    cause, recommendation, scenario = {
        ActionType.ROLLBACK_CONFIGURATION: (
            RootCause.DATABASE_POOL_MISCONFIGURATION,
            Recommendation.ROLLBACK_CONFIGURATION,
            DATABASE_POOL_SCENARIO_V2,
        ),
        ActionType.ROLLBACK_RELEASE: (
            RootCause.RELEASE_500_REGRESSION,
            Recommendation.ROLLBACK_RELEASE,
            RELEASE_500_SCENARIO,
        ),
        ActionType.RESTORE_DEPENDENCY: (
            RootCause.REDIS_UNAVAILABLE,
            Recommendation.RESTORE_DEPENDENCY,
            REDIS_UNAVAILABLE_SCENARIO_V2,
        ),
    }[action]
    return propose_action_plan(
        service_name="order-service",
        root_cause=cause,
        recommendation=recommendation,
        hypothesis_status=HypothesisStatus.SUPPORTED,
        evidence_items=scenario.evidence,
    ).steps[0]


def decision_for(*, questions=("仍需排除其他故障",)):
    request_id, job_id = uuid4(), uuid4()
    content = DecisionContent(
        request_id=request_id,
        plan_id=uuid4(),
        plan_content_sha256="a" * 64,
        candidate_job_id=job_id,
        source_content_sha256="b" * 64,
        wait_version=None,
        outcome="approved",
        evidence_ids=("configuration-1",),
        preconditions=("必须保留现有连接", "操作前核对数据库健康"),
        unresolved_questions=questions,
    )
    return DecisionRecord(
        id=uuid4(),
        investigation_run_id=uuid4(),
        candidate_job_id=job_id,
        request_id=request_id,
        plan_content_sha256=content.plan_content_sha256,
        previous_incident_version=3,
        incident_version=4,
        event_cursor=5,
        outcome="approved",
        policy_version="candidate-approval-decision-v1",
        decided_by_id="approver-1",
        authorization_id=uuid4(),
        idempotency_key="decision-test-1",
        command_sha256="c" * 64,
        content=content,
        content_sha256="d" * 64,
        created_at=NOW - timedelta(seconds=1),
    )


def observation_for(action=ActionType.ROLLBACK_CONFIGURATION, **changes):
    data = {
        "action_type": action,
        "observed_at": NOW,
        "source_path": OBSERVATION_PATHS[action],
        {
            ActionType.ROLLBACK_CONFIGURATION: "current_value",
            ActionType.RESTORE_DEPENDENCY: "available",
            ActionType.ROLLBACK_RELEASE: "release_enabled",
        }[action]: 5 if action is ActionType.ROLLBACK_CONFIGURATION else False,
    }
    return RuntimeObservation.model_validate({**data, **changes})


def command_data(**changes):
    return {
        "decision_id": uuid4(),
        "decision_content_sha256": "a" * 64,
        "expected_incident_version": 4,
        "idempotency_key": "preflight-test-1",
        "reason": "读取当前参数并保留原条件",
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_incident_version": 1},
        {"expected_incident_version": True},
        {"expected_incident_version": "4"},
        {"expected_incident_version": 4.0},
        {"decision_id": "invalid"},
        {"decision_content_sha256": "A" * 64},
        {"idempotency_key": "short"},
        {"idempotency_key": "invalid/key"},
        {"reason": " "},
        {"reason": "审" * 501},
        {"current_value": 5},
        {"conditions_satisfied": True},
        {"execution_eligible": True},
        {"url": "http://127.0.0.1"},
        {"parameters": {}},
    ],
)
def test_command_rejects_coercion_or_client_runtime_claims(changes):
    with pytest.raises(ValidationError):
        PreflightCommand.model_validate(command_data(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"current_value": "5"},
        {"current_value": 5.0},
        {"current_value": True},
        {"current_value": 0},
        {"current_value": 1001},
        {"current_value": None},
        {"available": False},
        {"release_enabled": True},
        {"source_path": "/__admin/dependencies/redis"},
        {"source_path": "http://127.0.0.1/__admin/configuration/database-pool"},
        {"observed_at": datetime(2026, 10, 25)},
        {"error_code": "fault_lab_timeout"},
        {"current_value": None, "error_code": "http://secret"},
        {"current_value": None, "error_code": "a" * 101},
    ],
)
def test_observation_rejects_wrong_shape_or_coercion(changes):
    with pytest.raises(ValidationError):
        observation_for(**changes)


@pytest.mark.parametrize("action", [ActionType.RESTORE_DEPENDENCY, ActionType.ROLLBACK_RELEASE])
@pytest.mark.parametrize("value", [1, 0, "true", "false"])
def test_boolean_observation_is_strict(action, value):
    field = "available" if action is ActionType.RESTORE_DEPENDENCY else "release_enabled"
    with pytest.raises(ValidationError):
        observation_for(action, **{field: value})


def test_successful_parameter_match_keeps_every_unverified_condition():
    decision, step = decision_for(), step_for()
    content = build_content(
        decision, step, observation_for(current_value=step.parameters.current_value)
    )
    assert content.preconditions == decision.content.preconditions
    assert content.unresolved_questions == decision.content.unresolved_questions
    assert content.blockers == ("unverified_preconditions", "unresolved_questions")
    assert content.decision_content_sha256 == decision.content_sha256
    assert content.plan_content_sha256 == decision.plan_content_sha256
    assert content.status == "blocked" and content.execution_eligible is False


def test_no_questions_does_not_remove_precondition_blocker():
    step = step_for()
    content = build_content(
        decision_for(questions=()),
        step,
        observation_for(current_value=step.parameters.current_value),
    )
    assert content.blockers == ("unverified_preconditions",)


@pytest.mark.parametrize("at_target", [False, True])
def test_configuration_drift_and_already_target_are_distinct_blockers(at_target):
    step = step_for()
    value = step.parameters.target_value if at_target else 999
    content = build_content(decision_for(), step, observation_for(current_value=value))
    assert content.blockers[-1] == (
        "already_at_target" if at_target else "configuration_current_value_mismatch"
    )


@pytest.mark.parametrize("available", [False, True])
def test_redis_current_state_never_implies_execute_permission(available):
    step = step_for(ActionType.RESTORE_DEPENDENCY)
    content = build_content(
        decision_for(), step, observation_for(step.action_type, available=available)
    )
    expected = ("unverified_preconditions", "unresolved_questions")
    assert content.blockers == expected + (("already_at_target",) if available else ())
    assert content.execution_eligible is False


@pytest.mark.parametrize("enabled", [False, True])
def test_release_switch_is_never_version_proof(enabled):
    step = step_for(ActionType.ROLLBACK_RELEASE)
    content = build_content(
        decision_for(), step, observation_for(step.action_type, release_enabled=enabled)
    )
    assert content.blockers == (
        "unverified_preconditions",
        "unresolved_questions",
        "runtime_version_unverifiable",
    )


@pytest.mark.parametrize("action", list(ActionType))
def test_observation_failure_keeps_source_constraints_and_adds_blocker(action):
    observation = RuntimeObservation(
        action_type=action,
        observed_at=NOW,
        source_path=OBSERVATION_PATHS[action],
        error_code="fault_lab_timeout",
    )
    content = build_content(decision_for(), step_for(action), observation)
    assert content.blockers[-1] == "runtime_observation_failed"
    assert "unverified_preconditions" in content.blockers
    assert "already_at_target" not in content.blockers


@pytest.mark.parametrize(
    "changes", [{"execution_eligible": True}, {"status": "ready"}, {"blockers": ()}]
)
def test_content_cannot_grant_execution_or_drop_all_blockers(changes):
    content = build_content(decision_for(), step_for(), observation_for())
    with pytest.raises(ValidationError):
        PreflightContent.model_validate({**content.model_dump(), **changes})


@pytest.mark.parametrize("outcome", ["rejected", "expired"])
def test_content_rejects_negative_decision(outcome):
    decision = decision_for().model_copy(update={"outcome": outcome})
    with pytest.raises(PreflightError, match="精确批准"):
        build_content(decision, step_for(), observation_for())


def test_content_rejects_observation_for_another_action():
    with pytest.raises(PreflightError):
        build_content(decision_for(), step_for(), observation_for(ActionType.RESTORE_DEPENDENCY))


@pytest.mark.parametrize("action", list(ActionType))
@pytest.mark.parametrize("target_name", ["other-service", "127.0.0.1", "shell"])
def test_whitelist_rejects_other_targets(action, target_name):
    step = step_for(action).model_copy(update={"target_name": target_name})
    with pytest.raises(PreflightError) as error:
        validate_step(step)
    assert error.value.code == "operation_not_whitelisted"


@pytest.mark.asyncio
async def test_invalid_step_is_rejected_before_network():
    calls = []
    observer = FaultLabPreflightObserver(
        transport=httpx2.MockTransport(lambda request: calls.append(request))
    )
    step = step_for().model_copy(update={"target_name": "other-service"})
    with pytest.raises(PreflightError):
        await observer.observe(step)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", list(ActionType))
async def test_real_fault_lab_http_contract_is_read_only(action):
    controller = FaultController(database_pool_size=5, redis_available=False)
    controller.set_release_500(enabled=True)
    before = controller.snapshot()
    observer = FaultLabPreflightObserver(
        ExecutionSettings(fault_lab_base_url="http://order-service"),
        transport=httpx2.ASGITransport(app=create_app(controller)),
        clock=lambda: NOW,
    )
    observed = await observer.observe(step_for(action))
    assert observed.error_code is None
    assert observed.observed_at == NOW
    assert observed.source_path == OBSERVATION_PATHS[action]
    assert controller.snapshot() == before


@pytest.mark.asyncio
async def test_http_method_path_and_origin_are_fixed():
    calls = []

    def handler(request):
        calls.append((request.method, str(request.url), request.content))
        return httpx2.Response(200, json={"name": "release_500", "enabled": True})

    observer = FaultLabPreflightObserver(transport=httpx2.MockTransport(handler), clock=lambda: NOW)
    observed = await observer.observe(step_for(ActionType.ROLLBACK_RELEASE))
    assert observed.release_enabled is True
    assert calls == [("GET", "http://127.0.0.1:18001/__admin/faults/release-500", b"")]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 307, 308, 404, 500])
async def test_http_failures_never_follow_redirects_or_copy_details(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(
            status, headers={"location": "https://example.invalid/private"}, text="secret detail"
        )

    observer = FaultLabPreflightObserver(transport=httpx2.MockTransport(handler), clock=lambda: NOW)
    observed = await observer.observe(step_for())
    assert observed.error_code == "fault_lab_unavailable"
    assert len(calls) == 1
    assert observed.current_value is None
    assert "secret" not in observed.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,payload",
    [
        (ActionType.ROLLBACK_RELEASE, {"name": "release_500", "enabled": "false"}),
        (ActionType.ROLLBACK_RELEASE, {"name": "release_500", "enabled": 0}),
        (ActionType.ROLLBACK_RELEASE, {"name": "other", "enabled": False}),
        (ActionType.RESTORE_DEPENDENCY, {"dependency": "redis", "available": "true"}),
        (ActionType.ROLLBACK_CONFIGURATION, {"key": "database_pool_size", "value": "5"}),
        (ActionType.ROLLBACK_CONFIGURATION, {"key": "database_pool_size", "value": True}),
        (ActionType.ROLLBACK_CONFIGURATION, {"key": "database_pool_size", "value": 5.0}),
        (ActionType.ROLLBACK_CONFIGURATION, {"key": "database_pool_size", "value": 0}),
        (ActionType.ROLLBACK_CONFIGURATION, {"key": "database_pool_size", "value": 1001}),
        (ActionType.ROLLBACK_RELEASE, {"enabled": False, "secret": "detail"}),
    ],
)
async def test_http_invalid_shapes_or_coercion_become_bounded_error(action, payload):
    if action is ActionType.RESTORE_DEPENDENCY:
        payload = {"redis_unavailable_ratio": 0.0, "status": "healthy", **payload}
    elif action is ActionType.ROLLBACK_CONFIGURATION:
        payload = {
            "database_pool_saturation": 1.0,
            "database_pool_waiters": 5,
            "status": "saturated",
            **payload,
        }
    observer = FaultLabPreflightObserver(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json=payload)),
        clock=lambda: NOW,
    )
    observed = await observer.observe(step_for(action))
    assert observed.error_code == "fault_lab_invalid_response"
    assert observed.current_value is observed.available is observed.release_enabled is None


class ResponseChunks(httpx2.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_stream_over_limit_stops_reading_and_closes_response():
    stream = ResponseChunks([b" " * 8192] * 20)
    observer = FaultLabPreflightObserver(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, stream=stream)),
        clock=lambda: NOW,
    )
    observed = await observer.observe(step_for())
    assert observed.error_code == "fault_lab_invalid_response"
    assert stream.reads == MAX_RESPONSE_BYTES // 8192 + 1
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("length", ["65537", "invalid", "-1"])
async def test_bad_content_length_rejects_before_reading_body(length):
    stream = ResponseChunks([b"secret"])
    observer = FaultLabPreflightObserver(
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, headers={"content-length": length}, stream=stream)
        ),
        clock=lambda: NOW,
    )
    observed = await observer.observe(step_for())
    assert observed.error_code == "fault_lab_invalid_response"
    assert stream.reads == 0 and stream.closed


@pytest.mark.asyncio
async def test_compressed_body_is_rejected_before_decompression():
    stream = ResponseChunks([b"not compressed"])
    observer = FaultLabPreflightObserver(
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, headers={"content-encoding": "gzip"}, stream=stream)
        ),
        clock=lambda: NOW,
    )
    observed = await observer.observe(step_for())
    assert observed.error_code == "fault_lab_invalid_response"
    assert stream.reads == 0 and stream.closed


@pytest.mark.asyncio
async def test_exact_response_limit_is_accepted():
    payload = b'{"name":"release_500","enabled":true}'
    payload += b" " * (MAX_RESPONSE_BYTES - len(payload))
    observer = FaultLabPreflightObserver(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, content=payload)),
        clock=lambda: NOW,
    )
    observed = await observer.observe(step_for(ActionType.ROLLBACK_RELEASE))
    assert observed.error_code is None and observed.release_enabled is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx2.ConnectError, httpx2.ReadTimeout])
async def test_network_errors_are_normalized_without_exception_details(failure):
    def handler(request):
        raise failure("credential=secret", request=request)

    observer = FaultLabPreflightObserver(transport=httpx2.MockTransport(handler), clock=lambda: NOW)
    observed = await observer.observe(step_for())
    assert observed.error_code == (
        "fault_lab_timeout" if failure is httpx2.ReadTimeout else "fault_lab_unavailable"
    )
    assert "credential" not in observed.model_dump_json()


@pytest.mark.asyncio
async def test_total_timeout_covers_transport_that_ignores_http_timeout():
    entered = asyncio.Event()

    async def handler(_):
        entered.set()
        await asyncio.Event().wait()

    observer = FaultLabPreflightObserver(
        ExecutionSettings(request_timeout_seconds=0.01),
        transport=httpx2.MockTransport(handler),
        clock=lambda: NOW,
    )
    observed = await asyncio.wait_for(observer.observe(step_for()), timeout=1)
    assert entered.is_set()
    assert observed.error_code == "fault_lab_timeout"


@pytest.mark.asyncio
async def test_total_timeout_covers_body_read_and_closes_stream():
    class SlowBody(httpx2.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"{"
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    stream = SlowBody()
    observer = FaultLabPreflightObserver(
        ExecutionSettings(request_timeout_seconds=0.01),
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, stream=stream)),
        clock=lambda: NOW,
    )
    observed = await asyncio.wait_for(observer.observe(step_for()), timeout=1)
    assert observed.error_code == "fault_lab_timeout"
    assert stream.closed


def test_observer_rechecks_settings_before_any_network_access():
    settings = ExecutionSettings().model_copy(
        update={"fault_lab_base_url": "https://example.invalid"}
    )
    with pytest.raises(ValidationError):
        FaultLabPreflightObserver(settings)


@pytest.mark.asyncio
async def test_observations_run_concurrently_without_blocking_event_loop():
    entered = 0
    both_entered = asyncio.Event()

    async def handler(_):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await both_entered.wait()
        return httpx2.Response(200, json={"enabled": False})

    observer = FaultLabPreflightObserver(transport=httpx2.MockTransport(handler), clock=lambda: NOW)
    step = step_for(ActionType.ROLLBACK_RELEASE)
    results = await asyncio.wait_for(
        asyncio.gather(observer.observe(step), observer.observe(step)), timeout=1
    )
    assert entered == 2
    assert all(result.error_code is None for result in results)


@pytest.mark.asyncio
async def test_cancellation_propagates_instead_of_publishing_failed_observation():
    entered = asyncio.Event()

    async def handler(_):
        entered.set()
        await asyncio.Event().wait()

    observer = FaultLabPreflightObserver(transport=httpx2.MockTransport(handler))
    task = asyncio.create_task(observer.observe(step_for()))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
