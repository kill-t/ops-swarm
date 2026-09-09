"""证据读取策略启动与完整调用交互的只读 API 边界。"""

import json
from uuid import uuid4

import httpx2
import pytest

from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from opsswarm.incidents.persistence import IncidentStore
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.loop.contracts import (
    AssistantMessage,
    CallInteraction,
    EvidenceToolRequest,
    EvidenceToolResult,
    LoopModelResult,
    ReadEvidenceArguments,
    ToolCall,
    ToolMessage,
)
from tests.integration.test_incident_store import make_submission
from tests.loop_helpers import loop_policy, prepared_loop, reserve_loop
from tests.model_budget_helpers import model_policy

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_start_loop_policy_requires_function_calling_profile(
    migrated_database_url,
    database_session_factory,
):
    incident = await IncidentStore(database_session_factory).ingest_alert(
        make_submission("evidence-loop-start-api")
    )
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))
    policy = loop_policy().model_dump(mode="json")
    body = {
        "expected_version": 1,
        "actor_id": "engineer",
        "reason": "开启有界证据读取",
        "mode": "multi_agent",
        "workflow_version": "v2",
        "context_policy_version": "evidence-read-v1",
        "model_budget": policy,
    }
    url = f"/api/v1/incidents/{incident.incident.id}/actions/start-investigation"
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        for changed in (
            {"model_budget": None},
            {"model_budget": model_policy().model_dump(mode="json")},
        ):
            response = await client.post(url, json={**body, **changed})
            assert response.status_code == 422
            assert response.json()["error_code"] == "request_validation_error"

        response = await client.post(url, json=body)
        assert response.status_code == 200
        run = response.json()["investigation"]
        assert run["workflow_version"] == "v2"
        assert run["context_policy_version"] == "evidence-read-v1"
        assert run["prompt_version"] == "evidence-read-v1"
        assert run["model_budget"] == policy


async def test_call_interactions_are_complete_read_only_and_run_scoped(
    migrated_database_url,
    database_session_factory,
):
    prepared, packet, store, service = await prepared_loop(database_session_factory)
    assert packet is not None
    _, claim, _, _, task, attempt, _ = prepared
    run_id = claim.run.id
    catalog = json.loads(packet.model_input.messages[1].content)["evidence_catalog"]
    entry = catalog["entries"][0]
    arguments = ReadEvidenceArguments(
        evidence_id=entry["evidence_id"],
        snapshot_sha256=catalog["snapshot_sha256"],
    )

    model_grant = await reserve_loop(prepared, packet, store)
    assistant = AssistantMessage(
        content="",
        tool_calls=(
            ToolCall(
                id="read-1",
                name="read_evidence",
                args=arguments.model_dump(mode="json"),
            ),
        ),
    )
    model_result = LoopModelResult(messages=(assistant,))
    await store.settle(
        run_id,
        model_grant.record.id,
        lease_token=claim.work_item.lease_token,
        status="succeeded",
        call_result=model_result,
    )

    tool_request = EvidenceToolRequest(
        source_call_id=model_grant.record.id,
        tool_call_id="read-1",
        arguments=arguments,
    )
    tool_grant = await store.reserve(
        run_id,
        lease_token=claim.work_item.lease_token,
        task_id=task.id,
        task_attempt_id=attempt.id,
        operation=CallOperation.READ_LOG_EVIDENCE,
        invocation_index=0,
        tool_request=tool_request,
    )
    content = await service.read(model_grant.record.id, task.id, arguments)
    tool_result = EvidenceToolResult(
        message=ToolMessage(
            content=content,
            tool_call_id="read-1",
            name="read_evidence",
        )
    )
    await store.settle(
        run_id,
        tool_grant.record.id,
        lease_token=claim.work_item.lease_token,
        status="succeeded",
        call_result=tool_result,
    )

    budget = await CallBudgetQueries(database_session_factory).get_snapshot(run_id)
    old_query = next(call for call in budget.calls if call.operation == "query_logs")
    other, _, _, _ = await prepared_loop(database_session_factory, build_packet=False)
    other_run_id = other[1].run.id
    other_call = (
        await CallBudgetQueries(database_session_factory).get_snapshot(other_run_id)
    ).calls[0]
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))

    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        model_root = f"/api/v1/investigations/{run_id}/calls/{model_grant.record.id}"
        response = await client.get(model_root + "/interaction")
        expected_model = CallInteraction(
            investigation_run_id=run_id,
            call_id=model_grant.record.id,
            tool_request=None,
            result=model_result,
        )
        assert response.status_code == 200
        assert response.json() == expected_model.model_dump(mode="json")

        tool_root = f"/api/v1/investigations/{run_id}/calls/{tool_grant.record.id}"
        response = await client.get(tool_root + "/interaction")
        expected_tool = CallInteraction(
            investigation_run_id=run_id,
            call_id=tool_grant.record.id,
            tool_request=tool_request,
            result=tool_result,
        )
        assert response.status_code == 200
        assert response.json() == expected_tool.model_dump(mode="json")
        assert response.json()["tool_request"]["arguments"] == arguments.model_dump(mode="json")
        assert (await client.post(tool_root + "/interaction", json={})).status_code == 405

        old_root = f"/api/v1/investigations/{run_id}/calls/{old_query.id}"
        old = await client.get(old_root + "/interaction")
        assert old.status_code == 200
        assert old.json()["tool_request"] is None and old.json()["result"] is None

        model_input = await client.get(model_root + "/model-input")
        assert model_input.status_code == 200
        assert model_input.json() == packet.model_input.model_dump(mode="json")
        assert model_input.json()["version"] == "evidence-loop-input-v1"
        manifest = await client.get(model_root + "/context-manifest")
        assert manifest.status_code == 200
        assert manifest.json() == packet.manifest.model_dump(mode="json")
        assert manifest.json()["tool_names"] == ["read_evidence"]

        for scoped_run, call_id, status, error_code in (
            (other_run_id, model_grant.record.id, 404, "call_interaction_not_found"),
            (run_id, other_call.id, 404, "call_interaction_not_found"),
            (run_id, uuid4(), 404, "call_interaction_not_found"),
            (uuid4(), model_grant.record.id, 404, "investigation_run_not_found"),
            (run_id, "bad-id", 422, "request_validation_error"),
            ("bad-id", model_grant.record.id, 422, "request_validation_error"),
        ):
            response = await client.get(
                f"/api/v1/investigations/{scoped_run}/calls/{call_id}/interaction"
            )
            assert response.status_code == status
            assert response.json()["error_code"] == error_code

    spec = app.openapi()["paths"]["/api/v1/investigations/{run_id}/calls/{call_id}/interaction"]
    assert set(spec) == {"get"}
    assert set(spec["get"]["responses"]) >= {"200", "404", "422"}
