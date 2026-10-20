"""版本化调查手册在专业循环中的预算、审计和恢复边界。"""

import hashlib
import json

import httpx2
import pytest
from sqlalchemy import update

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.loop.agent import EvidenceLoopAnalyst
from opsswarm.investigation.loop.contracts import (
    AssistantMessage,
    LoopModelResult,
    LoopToolResult,
    RunbookToolRequest,
    ToolCall,
    ToolMessage,
)
from opsswarm.investigation.loop.service import EvidenceLoopService
from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.runbooks.contracts import (
    ReadRunbookArguments,
    RunbookCatalog,
    RunbookReadDetail,
    catalog_material_id,
    runbook_material_id,
)
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
from opsswarm.investigation.tasks.contracts import TaskOutcome
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries
from tests.loop_helpers import loop_policy, tool_response
from tests.model_budget_helpers import sdk_model
from tests.task_evidence_helpers import evidence_attempt, material, save_evidence

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _prepared(database_session_factory, role: EvidenceKind):
    policy = loop_policy()
    prepared = await evidence_attempt(
        database_session_factory,
        model_budget=policy,
        context_policy_version="runbook-read-v1",
        role=role,
    )
    await save_evidence(prepared)
    _, claim, clock, _, task, attempt, _ = prepared
    service = EvidenceLoopService(database_session_factory, claim.run.id)
    return policy, prepared, service, task, attempt, clock


async def test_unmatched_role_receives_traceable_empty_catalog_without_runbook_tool(
    database_session_factory,
):
    policy, prepared, service, task, attempt, _ = await _prepared(
        database_session_factory, EvidenceKind.LOG
    )
    _, claim, *_ = prepared

    packet = await service.prepare(
        task.id,
        attempt.id,
        CallOperation.ANALYZE_LOG,
        0,
        policy,
    )
    payload = json.loads(packet.model_input.messages[1].content)
    names = [item["function"]["name"] for item in packet.model_input.tool_schema["tools"]]

    assert packet.model_input.version == "runbook-loop-input-v1"
    assert packet.manifest.tool_names == ("read_evidence",)
    assert names == ["read_evidence", "SpecialistReport"]
    assert payload["runbook_catalog"]["entries"] == []
    assert [item.kind for item in packet.manifest.materials][-1] == "runbook_catalog"
    assert claim.run.working_summary_policy_version is None


async def test_change_specialist_reads_runbook_then_evidence_with_recoverable_manifest(
    database_session_factory,
):
    policy, prepared, service, task, attempt, clock = await _prepared(
        database_session_factory, EvidenceKind.CHANGE
    )
    _, claim, *_ = prepared
    request = claim.request.model_copy(
        update={"started_at": RELEASE_500_SCENARIO.request.started_at}
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(IncidentRow)
            .where(IncidentRow.id == claim.request.incident_id)
            .values(started_at=request.started_at)
        )

    queries = CallBudgetQueries(database_session_factory)
    sent: list[dict[str, object]] = []

    async def reply(raw_request):
        payload = json.loads(raw_request.content)
        sent.append(payload)
        snapshot = await queries.get_snapshot(claim.run.id)
        active = [
            item for item in snapshot.calls if item.kind == "model" and item.status == "reserved"
        ]
        assert len(active) == 1
        current = active[0]
        saved = await queries.get_model_input(claim.run.id, current.id)
        manifest = await queries.get_context_manifest(claim.run.id, current.id)
        assert saved.version == "runbook-loop-input-v1"
        assert manifest is not None
        data = json.loads(payload["messages"][1]["content"])
        runbook = data["runbook_catalog"]["entries"][0]
        evidence = data["evidence_catalog"]["entries"][0]
        material_kinds = tuple(item.kind for item in manifest.materials)
        if len(sent) == 1:
            assert manifest.tool_names == ("read_evidence", "read_runbook")
            assert "runbook_content" not in material_kinds
            return tool_response(
                "read_runbook",
                {
                    "runbook_id": runbook["runbook_id"],
                    "version": runbook["version"],
                    "content_sha256": runbook["content_sha256"],
                },
                call_id="runbook-1",
            )
        if len(sent) == 2:
            assert material_kinds[-1] == "runbook_content"
            detail = RunbookReadDetail.model_validate_json(payload["messages"][-1]["content"])
            content_material = manifest.materials[-1]
            assert content_material.material_id == detail.document.material_id
            assert content_material.content_sha256 == detail.document.content_sha256
            rebuilt = await service.prepare(
                task.id,
                attempt.id,
                CallOperation.ANALYZE_CHANGE,
                1,
                policy,
            )
            assert rebuilt == await service.prepare(
                task.id,
                attempt.id,
                CallOperation.ANALYZE_CHANGE,
                1,
                policy,
            )
            return tool_response(
                "read_evidence",
                {
                    "evidence_id": evidence["evidence_id"],
                    "snapshot_sha256": data["evidence_catalog"]["snapshot_sha256"],
                },
                call_id="evidence-2",
            )
        assert material_kinds[-1] == "runbook_content"
        return tool_response(
            "SpecialistReport",
            {
                "specialist": "change",
                "summary": "已按手册核对发布与事故时间关系。",
                "findings": ["近期存在已完成部署，仍保留竞争性解释。"],
                "evidence_ids": [evidence["evidence_id"]],
                "confidence": 0.75,
            },
            call_id="report-3",
        )

    runner = BudgetedCallRunner(
        run_id=claim.run.id,
        lease_token=claim.work_item.lease_token,
        store=CallBudgetStore(database_session_factory, clock=clock),
    )
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client:
        analyst = EvidenceLoopAnalyst(
            sdk_model(client),
            policy=policy,
            runner=runner,
            service=service,
        )
        with runner.task_scope(task.id, attempt.id):
            report = await analyst.analyze(
                specialist=EvidenceKind.CHANGE,
                request=request,
                evidence=material(EvidenceKind.CHANGE),
            )

    snapshot = await queries.get_snapshot(claim.run.id)
    assert report.evidence_ids == ("change-order-release-rc1",)
    assert snapshot.reserved_model_calls == 3
    assert snapshot.reserved_tool_calls == 3  # 首次取证、手册读取和证据读取
    assert [item.operation for item in snapshot.calls].count(CallOperation.READ_CHANGE_RUNBOOK) == 1
    assert [item.operation for item in snapshot.calls].count(
        CallOperation.READ_CHANGE_EVIDENCE
    ) == 1
    assert all(item.status == "succeeded" for item in snapshot.calls)


async def test_same_version_content_drift_fails_closed_before_retry_model_reservation(
    database_session_factory,
    monkeypatch,
):
    policy, prepared, service, task, attempt, clock = await _prepared(
        database_session_factory, EvidenceKind.CHANGE
    )
    _, claim, *_ = prepared
    request = claim.request.model_copy(
        update={"started_at": RELEASE_500_SCENARIO.request.started_at}
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(IncidentRow)
            .where(IncidentRow.id == claim.request.incident_id)
            .values(started_at=request.started_at)
        )

    first = await service.prepare(
        task.id,
        attempt.id,
        CallOperation.ANALYZE_CHANGE,
        0,
        policy,
    )
    catalog = RunbookCatalog.model_validate(
        json.loads(first.model_input.messages[1].content)["runbook_catalog"]
    )
    store = CallBudgetStore(database_session_factory, clock=clock)
    grant = await store.reserve(
        claim.run.id,
        lease_token=claim.work_item.lease_token,
        operation=CallOperation.ANALYZE_CHANGE,
        task_id=task.id,
        task_attempt_id=attempt.id,
        model_input=first.model_input,
        prompt_version=first.manifest.prompt_version,
        context_manifest=first.manifest,
    )
    await store.settle(
        claim.run.id,
        grant.record.id,
        lease_token=claim.work_item.lease_token,
        status="failed",
        error_code="model_transport_failed",
    )
    await prepared[3].finish_attempt(
        claim.run.id,
        task.id,
        attempt.id,
        lease_token=claim.work_item.lease_token,
        outcome=TaskOutcome(
            status="failed",
            error_code="model_transport_failed",
            retryable=True,
        ),
    )
    snapshot = await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
    retriable = next(item for item in snapshot.rounds[0].tasks if item.id == task.id)
    retry = await prepared[3].begin_attempt(
        claim.run.id,
        task.id,
        lease_token=claim.work_item.lease_token,
        expected_version=retriable.version,
    )
    assert retry.attempt is not None and retry.attempt.id != attempt.id

    entry = catalog.entries[0]
    drift_hash = "b" * 64
    drift_entry = entry.model_copy(
        update={
            "content_sha256": drift_hash,
            "material_id": runbook_material_id(entry.runbook_id, entry.version, drift_hash),
        }
    )
    catalog_hash = hashlib.sha256(
        json.dumps(
            [drift_entry.model_dump(mode="json")],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    drifted = RunbookCatalog(
        material_id=catalog_material_id(catalog.version, catalog_hash),
        content_sha256=catalog_hash,
        entries=(drift_entry,),
    )
    monkeypatch.setattr(
        "opsswarm.investigation.loop.context.select_runbooks",
        lambda *_args, **_kwargs: drifted,
    )

    with pytest.raises(TaskFactConflict, match="手册目录与当前版本不一致"):
        await service.prepare(
            task.id,
            retry.attempt.id,
            CallOperation.ANALYZE_CHANGE,
            0,
            policy,
        )
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert budget.reserved_model_calls == 1
    assert budget.reserved_tool_calls == 1  # 只有准备任务时的首次取证


async def test_runbook_ledger_rejects_out_of_catalog_request_and_tampered_result(
    database_session_factory,
):
    policy, prepared, service, task, attempt, clock = await _prepared(
        database_session_factory, EvidenceKind.CHANGE
    )
    _, claim, *_ = prepared
    request = claim.request.model_copy(
        update={"started_at": RELEASE_500_SCENARIO.request.started_at}
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(IncidentRow)
            .where(IncidentRow.id == claim.request.incident_id)
            .values(started_at=request.started_at)
        )
    packet = await service.prepare(
        task.id,
        attempt.id,
        CallOperation.ANALYZE_CHANGE,
        0,
        policy,
    )
    catalog = RunbookCatalog.model_validate(
        json.loads(packet.model_input.messages[1].content)["runbook_catalog"]
    )
    entry = catalog.entries[0]
    arguments = ReadRunbookArguments(
        runbook_id=entry.runbook_id,
        version=entry.version,
        content_sha256=entry.content_sha256,
    )
    store = CallBudgetStore(database_session_factory, clock=clock)
    model = await store.reserve(
        claim.run.id,
        lease_token=claim.work_item.lease_token,
        task_id=task.id,
        task_attempt_id=attempt.id,
        operation=CallOperation.ANALYZE_CHANGE,
        model_input=packet.model_input,
        context_manifest=packet.manifest,
        prompt_version=packet.manifest.prompt_version,
    )
    await store.settle(
        claim.run.id,
        model.record.id,
        lease_token=claim.work_item.lease_token,
        status="succeeded",
        call_result=LoopModelResult(
            messages=(
                AssistantMessage(
                    content="",
                    tool_calls=(
                        ToolCall(
                            id="runbook-ledger",
                            name="read_runbook",
                            args=arguments.model_dump(mode="json"),
                        ),
                    ),
                ),
            )
        ),
    )
    forged = RunbookToolRequest(
        source_call_id=model.record.id,
        tool_call_id="runbook-ledger",
        arguments=arguments.model_copy(update={"content_sha256": "0" * 64}),
    )
    with pytest.raises(TaskFactConflict):
        await store.reserve(
            claim.run.id,
            lease_token=claim.work_item.lease_token,
            task_id=task.id,
            task_attempt_id=attempt.id,
            operation=CallOperation.READ_CHANGE_RUNBOOK,
            tool_request=forged,
        )

    tool_request = forged.model_copy(update={"arguments": arguments})
    tool = await store.reserve(
        claim.run.id,
        lease_token=claim.work_item.lease_token,
        task_id=task.id,
        task_attempt_id=attempt.id,
        operation=CallOperation.READ_CHANGE_RUNBOOK,
        tool_request=tool_request,
    )
    content = await service.read_runbook(model.record.id, task.id, attempt.id, arguments)
    detail = RunbookReadDetail.model_validate_json(content)
    tampered = detail.model_copy(
        update={"document": detail.document.model_copy(update={"title": "被篡改的标题"})}
    )
    result = LoopToolResult(
        message=ToolMessage(
            content=tampered.model_dump_json(),
            name="read_runbook",
            tool_call_id="runbook-ledger",
        )
    )
    with pytest.raises(TaskFactConflict):
        await store.settle(
            claim.run.id,
            tool.record.id,
            lease_token=claim.work_item.lease_token,
            status="succeeded",
            call_result=result,
        )
