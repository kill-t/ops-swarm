"""摘要输入的完整材料、来源身份与最终预算边界。"""

import json
from dataclasses import replace
from uuid import uuid4

import pytest

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_contracts import TextModelMessage
from opsswarm.investigation.budget.model_policy import allocate_input
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.context.builder import ContextBuilder, content_hash
from opsswarm.investigation.context.catalog import catalog_group
from opsswarm.investigation.context.dispatch import build_context
from opsswarm.investigation.context.summary_builder import SummaryContextBuilder
from opsswarm.investigation.models import EvidenceKind, SpecialistReport
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.results import SpecialistTaskResult, TaskResultRecord
from opsswarm.investigation.working_summary.contracts import (
    SummaryCallReference,
    TaskWorkingSummary,
    WorkingSummaryRecord,
)
from tests.context_helpers import sources, supplied_input
from tests.model_budget_helpers import model_policy


def summary_sources():
    base = sources()
    tasks = []
    for role in EvidenceKind:
        task_id, attempt_id, call_id = uuid4(), uuid4(), uuid4()
        original = base.tasks[0]
        assignment = original.assignment.model_copy(update={"role": role, "task_key": role.value})
        snapshot = original.evidence.snapshot.model_copy(
            update={
                "role": role,
                "evidence": tuple(
                    e.model_copy(update={"kind": role, "evidence_id": f"{role.value}-{i}"})
                    for i, e in enumerate(original.evidence.snapshot.evidence)
                ),
            }
        )
        evidence = original.evidence.model_copy(
            update={
                "task_id": task_id,
                "snapshot": snapshot,
                "content_sha256": content_hash(snapshot),
            }
        )
        report = SpecialistReport(
            specialist=role,
            summary="保留专业判断",
            findings=("支持：存在异常", "反证：另一条材料正常", "未解问题：因果仍待确认"),
            evidence_ids=(snapshot.evidence[0].evidence_id,),
            confidence=0.5,
        )
        result = TaskResultRecord(
            task_id=task_id,
            investigation_run_id=base.run_id,
            attempt_id=attempt_id,
            result=SpecialistTaskResult(
                evidence_snapshot_id=snapshot.evidence_snapshot_id,
                report=report,
                evidence=snapshot.evidence,
            ),
            created_at=evidence.created_at,
        )
        summary = TaskWorkingSummary(
            investigation_run_id=base.run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            assignment=assignment,
            report=report,
            result_sha256=content_hash(result.result),
            source_call_id=call_id,
            source_result_sha256="a" * 64,
            evidence_catalog=catalog_group(evidence),
            read_evidence_ids=report.evidence_ids,
            calls=(
                SummaryCallReference(
                    call_id=uuid4(),
                    attempt_id=uuid4(),
                    sequence=1,
                    invocation_index=0,
                    kind="model",
                    operation=f"analyze_{role.value}",
                    status="unknown",
                    error_code="parent_attempt_ended",
                ),
                SummaryCallReference(
                    call_id=call_id,
                    attempt_id=attempt_id,
                    sequence=2,
                    invocation_index=1,
                    kind="model",
                    operation=f"analyze_{role.value}",
                    status="succeeded",
                    error_code=None,
                ),
            ),
        )
        record = WorkingSummaryRecord(
            task_id=task_id,
            investigation_run_id=base.run_id,
            attempt_id=attempt_id,
            version=summary.version,
            summary=summary,
            content_sha256=content_hash(summary),
            created_at=evidence.created_at,
        )
        tasks.append(
            replace(
                original,
                task_id=task_id,
                assignment=assignment,
                evidence=evidence,
                result=result,
                working_summary=record,
            )
        )
    return replace(
        base, task_id=None, tasks=tuple(tasks), context_policy_version="summary-context-v1"
    )


def policy():
    return model_policy(max_input_tokens_per_call=60000)


def test_summary_context_keeps_complete_evidence_and_rebuilds_exact_input():
    material = summary_sources()
    packet = build_context(supplied_input(material), material, CallOperation.SYNTHESIZE, policy())
    payload = json.loads(packet.model_input.messages[1].content)
    assert "reports" not in payload
    assert len(payload["working_summaries"]) == 3
    assert payload["evidence"] == sorted(
        [e.model_dump(mode="json") for t in material.tasks for e in t.evidence.snapshot.evidence],
        key=lambda e: e["evidence_id"],
    )
    for record in payload["working_summaries"]:
        assert record["summary"]["report"]["findings"][-1].startswith("未解问题")
        assert record["summary"]["calls"][0]["status"] == "unknown"
        assert record["summary"]["read_evidence_ids"] == record["summary"]["report"]["evidence_ids"]
    assert len(packet.manifest.materials) == 10
    assert len([m for m in packet.manifest.materials if m.kind == "task_working_summary"]) == 3
    assert not any(m.kind == "task_result" for m in packet.manifest.materials)
    for reference in (m for m in packet.manifest.materials if m.kind == "task_working_summary"):
        summary = next(s for s in material.tasks if s.task_id == reference.material_id)
        assert reference.evidence_ids == summary.working_summary.summary.read_evidence_ids
    assert packet.manifest.tool_names == packet.manifest.trimming == ()
    assert packet.manifest.allocation == allocate_input(
        policy(), packet.model_input, prompt_version="summary-context-v1"
    )
    assert build_context(packet.model_input, material, CallOperation.SYNTHESIZE, policy()) == packet


@pytest.mark.parametrize("change", ["missing", "hash", "result", "catalog", "attempt"])
def test_rejects_missing_or_inconsistent_summary_sources(change):
    material = summary_sources()
    task = material.tasks[0]
    record = task.working_summary
    if change == "missing":
        record = None
    elif change == "hash":
        record = record.model_copy(update={"content_sha256": "0" * 64})
    else:
        summary = record.summary
        updates = (
            {"result_sha256": "0" * 64}
            if change == "result"
            else (
                {
                    "evidence_catalog": summary.evidence_catalog.model_copy(
                        update={"snapshot_sha256": "0" * 64}
                    )
                }
                if change == "catalog"
                else {"attempt_id": uuid4()}
            )
        )
        summary = summary.model_copy(update=updates)
        record = record.model_copy(
            update={"summary": summary, "content_sha256": content_hash(summary)}
        )
    altered = replace(material, tasks=(replace(task, working_summary=record), *material.tasks[1:]))
    with pytest.raises(TaskFactConflict):
        build_context(supplied_input(material), altered, CallOperation.SYNTHESIZE, policy())


@pytest.mark.parametrize("change", ["summary", "evidence", "duplicate_report", "extra"])
def test_rejects_modified_assembled_payload(change):
    material = summary_sources()
    packet = build_context(supplied_input(material), material, CallOperation.SYNTHESIZE, policy())
    payload = json.loads(packet.model_input.messages[1].content)
    if change == "summary":
        payload["working_summaries"][0]["created_at"] = "2026-09-10T00:00:00Z"
    elif change == "evidence":
        payload["evidence"][0]["summary"] = "删除了反证"
    elif change == "duplicate_report":
        payload["reports"] = []
    else:
        payload["unexpected"] = True
    supplied = packet.model_input.model_copy(
        update={
            "messages": (
                packet.model_input.messages[0],
                TextModelMessage(role="user", content=json.dumps(payload)),
            )
        }
    )
    with pytest.raises(TaskFactConflict):
        build_context(supplied, material, CallOperation.SYNTHESIZE, policy())


def test_budget_checks_complete_final_input_once(monkeypatch):
    material = summary_sources()
    supplied = supplied_input(material)
    packet = build_context(supplied, material, CallOperation.SYNTHESIZE, policy())
    count = 0

    def counted(*args, **kwargs):
        nonlocal count
        count += 1
        return allocate_input(*args, **kwargs)

    monkeypatch.setattr("opsswarm.investigation.context.summary_builder.allocate_input", counted)
    exact = policy().model_copy(
        update={"max_input_tokens_per_call": packet.manifest.allocation.estimated_input_tokens}
    )
    assert build_context(supplied, material, CallOperation.SYNTHESIZE, exact) == packet
    assert count == 1
    with pytest.raises(CallBudgetExceeded) as caught:
        build_context(
            supplied,
            material,
            CallOperation.SYNTHESIZE,
            exact.model_copy(
                update={"max_input_tokens_per_call": exact.max_input_tokens_per_call - 1}
            ),
        )
    assert count == 2
    assert caught.value.code == "model_input_budget_exceeded"


def test_legacy_dispatch_preserves_serialization_and_does_not_consume_summaries():
    material = replace(summary_sources(), context_policy_version="task-context-v1")
    supplied = supplied_input(material)
    expected = ContextBuilder().build(supplied, material, CallOperation.SYNTHESIZE, policy())
    actual = build_context(supplied, material, CallOperation.SYNTHESIZE, policy())
    assert actual.model_dump_json() == expected.model_dump_json()
    payload = json.loads(actual.model_input.messages[1].content)
    assert "reports" in payload and "working_summaries" not in payload
    with pytest.raises(TaskFactConflict):
        SummaryContextBuilder().build(supplied, material, CallOperation.SYNTHESIZE, policy())


def with_long_call_history(material, count):
    tasks = []
    for task in material.tasks:
        record = task.working_summary
        summary = record.summary
        summary = summary.model_copy(
            update={
                "calls": (
                    *summary.calls,
                    *(
                        summary.calls[0].model_copy(
                            update={
                                "call_id": uuid4(),
                                "sequence": index + 3,
                                "error_code": "未" * 100,
                                "operation": "a" * 32,
                            }
                        )
                        for index in range(count)
                    ),
                )
            }
        )
        record = record.model_copy(
            update={"summary": summary, "content_sha256": content_hash(summary)}
        )
        tasks.append(replace(task, working_summary=record))
    return replace(material, tasks=tuple(tasks))


def test_large_chinese_history_rebuild_uses_actual_utf8_payload():
    material = with_long_call_history(summary_sources(), 112)
    large = policy().model_copy(
        update={
            "max_input_tokens_per_call": 262144,
            "profile": policy().profile.model_copy(update={"context_window_tokens": 300000}),
        }
    )
    packet = build_context(supplied_input(material), material, CallOperation.SYNTHESIZE, large)
    payload = json.loads(packet.model_input.messages[1].content)
    assert len(json.dumps(payload)) > 262144
    assert build_context(packet.model_input, material, CallOperation.SYNTHESIZE, large) == packet


def test_oversized_final_input_has_stable_budget_failure():
    material = with_long_call_history(summary_sources(), 158)
    with pytest.raises(CallBudgetExceeded) as caught:
        build_context(supplied_input(material), material, CallOperation.SYNTHESIZE, policy())
    assert caught.value.code == "model_input_too_large"
