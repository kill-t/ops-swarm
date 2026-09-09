"""真实框架和 SDK 在 PostgreSQL 预算下完成专业读取或明确停止。"""

import json
from datetime import timedelta

import httpx2
import pytest
from langchain.agents.structured_output import StructuredOutputValidationError
from openai import APITimeoutError

from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.loop.agent import EvidenceLoopAnalyst
from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.tasks.errors import TaskFactConflict
from tests.loop_helpers import loop_policy, prepared_loop, tool_response
from tests.model_budget_helpers import sdk_model
from tests.task_evidence_helpers import material

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "two_reads",
        "unknown",
        "illegal_tool",
        "foreign_evidence",
        "bad_hash",
        "extra_scope",
        "unread_report",
        "invalid_report",
        "oversized",
        "repeated_id",
        "count",
        "input",
        "output",
        "cost",
        "task_tools",
        "timeout",
        "bad_usage",
        "deadline",
    ],
)
async def test_specialist_loop_commits_each_boundary_before_next_call(
    database_session_factory, outcome
):
    changes = {}
    if outcome == "input":
        changes["max_input_tokens_per_call"] = 1000
    elif outcome == "output":
        changes["max_output_tokens"] = 1024
    elif outcome == "cost":
        changes["max_estimated_cost_usd"] = "0.000001"
    policy = loop_policy(**changes)
    calls = CallBudgetPolicy(
        max_model_calls=1 if outcome == "count" else 12,
        max_tool_calls_per_task=1 if outcome == "task_tools" else 6,
        max_duration_seconds=5 if outcome == "deadline" else 300,
    )
    prepared, _, store, service = await prepared_loop(
        database_session_factory, policy=policy, call_budget=calls, build_packet=False
    )
    _, claim, clock, _, task, attempt, _ = prepared
    queries = CallBudgetQueries(database_session_factory)
    sent = []

    async def reply(request):
        payload = json.loads(request.content)
        sent.append(payload)
        snapshot = await queries.get_snapshot(claim.run.id)
        active = [c for c in snapshot.calls if c.kind == "model" and c.status == "reserved"]
        assert len(active) == 1 and active[0].invocation_index == len(sent) - 1
        saved = await queries.get_model_input(claim.run.id, active[0].id)
        assert saved.version == "evidence-loop-input-v1"
        assert payload["tools"] == saved.tool_schema["tools"]
        assert payload["parallel_tool_calls"] is False and payload["max_completion_tokens"] == 1024
        assert payload["tool_choice"] == saved.tool_schema["tool_choice"] == "required"
        data = json.loads(payload["messages"][1]["content"])
        catalog = data["evidence_catalog"]
        assert "attributes" not in payload["messages"][1]["content"]
        entry = catalog["entries"][0]
        if outcome == "timeout":
            raise httpx2.ReadTimeout("模拟模型超时")
        if outcome == "deadline":
            clock.current += timedelta(seconds=6)
        if len(sent) == 1:
            if outcome in ("unread_report", "invalid_report"):
                report = {
                    "specialist": "log",
                    "summary": "未读取",
                    "findings": ["缺口"],
                    "evidence_ids": [entry["evidence_id"]],
                    "confidence": 0.5,
                }
                if outcome == "invalid_report":
                    report.pop("summary")
                return tool_response("SpecialistReport", report)
            arguments = {
                "evidence_id": entry["evidence_id"],
                "snapshot_sha256": catalog["snapshot_sha256"],
            }
            if outcome == "foreign_evidence":
                arguments["evidence_id"] = "other-incident-evidence"
            elif outcome == "bad_hash":
                arguments["snapshot_sha256"] = "0" * 64
            elif outcome == "extra_scope":
                arguments["task_id"] = str(task.id)
            elif outcome == "oversized":
                arguments["evidence_id"] = "x" * 40000
            response = tool_response(
                "shell" if outcome == "illegal_tool" else "read_evidence",
                arguments,
                usage=outcome != "unknown",
            )
            if outcome == "bad_usage":
                data = response.json()
                data["usage"]["total_tokens"] = 101
                return httpx2.Response(200, json=data)
            return response
        tool_rows = [c for c in snapshot.calls if c.operation == "read_log_evidence"]
        assert len(tool_rows) == len(sent) - 1 and all(c.status == "succeeded" for c in tool_rows)
        interaction = await queries.get_interaction(claim.run.id, tool_rows[-1].id)
        assert payload["messages"][-1]["tool_call_id"] == interaction.result.message.tool_call_id
        detail = json.loads(payload["messages"][-1]["content"])
        assert detail["evidence"]["evidence_id"] == entry["evidence_id"]
        if outcome == "two_reads" and len(sent) == 2:
            return tool_response(
                "read_evidence",
                {
                    "evidence_id": entry["evidence_id"],
                    "snapshot_sha256": catalog["snapshot_sha256"],
                },
                call_id="read-2",
            )
        return tool_response(
            "SpecialistReport",
            {
                "specialist": "log",
                "summary": "完成按需读取",
                "findings": ["保留证据与未解问题"],
                "evidence_ids": [entry["evidence_id"]],
                "confidence": 0.8,
            },
            call_id="tool-1" if outcome == "repeated_id" else "report-2",
            usage=outcome != "unknown",
        )

    runner = BudgetedCallRunner(
        run_id=claim.run.id, lease_token=claim.work_item.lease_token, store=store
    )
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client:
        analyst = EvidenceLoopAnalyst(
            sdk_model(client), policy=policy, runner=runner, service=service
        )
        with runner.task_scope(task.id, attempt.id):
            if outcome in ("success", "unknown", "two_reads"):
                report = await analyst.analyze(
                    specialist=EvidenceKind.LOG, request=claim.request, evidence=material()
                )
                assert len(report.evidence_ids) == 1
            else:
                with pytest.raises(
                    (
                        ValueError,
                        TaskFactConflict,
                        CallBudgetExceeded,
                        StructuredOutputValidationError,
                        APITimeoutError,
                    )
                ):
                    await analyst.analyze(
                        specialist=EvidenceKind.LOG, request=claim.request, evidence=material()
                    )
    snapshot = await queries.get_snapshot(claim.run.id)
    if outcome in ("success", "unknown", "two_reads"):
        expected = 3 if outcome == "two_reads" else 2
        assert (
            len(sent) == expected
            and snapshot.reserved_model_calls == expected
            and snapshot.reserved_tool_calls == expected
        )
        assert all(c.status == "succeeded" for c in snapshot.calls)
        assert snapshot.model_usage.unknown_model_calls == (2 if outcome == "unknown" else 0)
        models = [c for c in snapshot.calls if c.kind == "model"]
        result = (await queries.get_interaction(claim.run.id, models[-1].id)).result
        assert (
            len(result.messages) == 2
            and result.messages[0].tool_calls[0].id == result.messages[1].tool_call_id
        )
    elif outcome in ("input", "cost"):
        assert sent == [] and snapshot.reserved_model_calls == 0
    elif outcome == "repeated_id":
        assert len(sent) == 2
    else:
        assert len(sent) == 1
    if outcome in (
        "illegal_tool",
        "extra_scope",
        "oversized",
        "unread_report",
        "invalid_report",
        "bad_usage",
    ):
        assert snapshot.reserved_tool_calls == 1
        assert [c for c in snapshot.calls if c.kind == "model"][0].status == "failed"
    if outcome == "invalid_report":
        assert snapshot.model_usage.reported_input_tokens == 80
