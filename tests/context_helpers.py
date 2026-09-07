"""上下文测试的不可变材料和固定图输入。"""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from langchain_core.utils.function_calling import convert_to_openai_tool

from opsswarm.investigation.budget.model_contracts import ModelInput, TextModelMessage
from opsswarm.investigation.context.contracts import ContextSources, ContextTaskMaterial
from opsswarm.investigation.models import (
    EvidenceKind,
    InvestigationConclusion,
    InvestigationRequest,
    SpecialistReport,
)
from opsswarm.investigation.parallel_prompts import conclusion_messages, specialist_messages
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.evidence import TaskEvidenceRecord, evidence_hash
from tests.task_evidence_helpers import snapshot


def sources():
    material = snapshot()
    run_id, task_id = uuid4(), uuid4()
    request = InvestigationRequest(
        incident_id=uuid4(),
        service_name=material.service_name,
        title="上下文任务",
        started_at=material.window_end - timedelta(minutes=5),
    )
    task = ContextTaskMaterial(
        task_id=task_id,
        assignment=TaskAssignment(
            task_key="log",
            role="log",
            goal="分析日志反证和支持证据",
            service_name=material.service_name,
            window_start=material.window_start,
            window_end=material.window_end,
            evidence_snapshot_id=material.evidence_snapshot_id,
        ),
        evidence=TaskEvidenceRecord(
            task_id=task_id,
            investigation_run_id=run_id,
            attempt_id=uuid4(),
            call_id=uuid4(),
            snapshot=material,
            content_sha256=evidence_hash(material),
            created_at=material.window_end,
        ),
        result=None,
    )
    return ContextSources(
        run_id=run_id,
        task_id=task_id,
        request=request,
        evidence_policy_version=material.version,
        tasks=(task,),
    )


def supplied_input(materials, method="json_schema"):
    evidence = tuple(e for t in materials.tasks for e in t.evidence.snapshot.evidence)
    if materials.task_id:
        messages = specialist_messages(EvidenceKind.LOG, materials.request, evidence, method)
        schema = SpecialistReport
    else:
        messages = conclusion_messages(
            materials.request,
            evidence,
            tuple(t.result.result.report for t in materials.tasks if t.result),
            method,
        )
        schema = InvestigationConclusion
    return ModelInput(
        messages=tuple(
            TextModelMessage(
                role="system" if m.type == "system" else "user",
                content=m.content,
            )
            for m in messages
        ),
        output_schema=schema.model_json_schema(),
        tool_schema=convert_to_openai_tool(schema),
        structured_output_method=method,
    )


def with_evidence(materials, evidence):
    task = materials.tasks[0]
    snapshot = task.evidence.snapshot.model_copy(update={"evidence": tuple(evidence)})
    record = task.evidence.model_copy(
        update={"snapshot": snapshot, "content_sha256": evidence_hash(snapshot)}
    )
    return replace(materials, tasks=(replace(task, evidence=record),))
