"""为独立审查组装与原综合调用精确同源的材料。"""

import json
from typing import Literal

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_contracts import (
    ModelBudgetPolicy,
    ModelInput,
    TextModelMessage,
)
from opsswarm.investigation.budget.model_policy import allocate_input
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.context.catalog import catalog_group
from opsswarm.investigation.context.contracts import (
    ContextManifest,
    ContextMaterial,
    ContextPacket,
    ContextSources,
)
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.context.prompts import (
    FOLLOW_UP_HYPOTHESIS_REVIEW_PROMPT,
    HYPOTHESIS_REVIEW_PROMPT,
    THIRD_ROUND_HYPOTHESIS_REVIEW_PROMPT,
)
from opsswarm.investigation.context.validation import validate_context
from opsswarm.investigation.synthesis.contracts import OpenHypothesisRecord
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.working_summary.contracts import WorkingSummaryRecord

REVIEW_PROMPT_VERSION: Literal["open-hypothesis-review-v1"] = "open-hypothesis-review-v1"
FOLLOW_UP_REVIEW_PROMPT_VERSION: Literal["follow-up-open-hypothesis-review-v1"] = (
    "follow-up-open-hypothesis-review-v1"
)
THIRD_ROUND_REVIEW_PROMPT_VERSION: Literal["third-round-open-hypothesis-review-v1"] = (
    "third-round-open-hypothesis-review-v1"
)
ReviewPromptVersion = Literal[
    "open-hypothesis-review-v1",
    "follow-up-open-hypothesis-review-v1",
    "third-round-open-hypothesis-review-v1",
]
ReviewRoleVersion = Literal[
    "independent-review-v1",
    "independent-follow-up-review-v1",
    "independent-third-round-review-v1",
]


class ReviewContextBuilder:
    """核对假设来源，并为审查调用形成不可扩大范围的上下文。"""

    def build(
        self,
        supplied: ModelInput,
        sources: ContextSources,
        operation: CallOperation,
        policy: ModelBudgetPolicy,
    ) -> ContextPacket:
        if (
            sources.context_policy_version != "summary-context-v1"
            or sources.workflow_version != "v3"
            or operation != CallOperation.REVIEW
            or sources.open_hypothesis is None
            or sources.source_synthesis_manifest is None
            or sources.round_number not in (1, 2, 3)
        ):
            raise TaskFactConflict("context_operation_invalid", "独立审查缺少固定的假设来源")
        hypothesis = OpenHypothesisRecord.model_validate(
            sources.open_hypothesis.model_dump(mode="json")
        )
        tasks = sorted(sources.tasks, key=lambda task: task.assignment.role.value)
        summaries: list[WorkingSummaryRecord] = []
        for task in tasks:
            try:
                if task.working_summary is None or task.result is None:
                    raise ValueError("缺少工作摘要或结果")
                record = WorkingSummaryRecord.model_validate(
                    task.working_summary.model_dump(mode="json")
                )
                summary = record.summary
                if (
                    record.task_id != task.task_id
                    or record.investigation_run_id != sources.run_id
                    or record.attempt_id != task.result.attempt_id
                    or summary.assignment != task.assignment
                    or summary.report != task.result.result.report
                    or summary.result_sha256 != content_hash(task.result.result)
                    or task.evidence.content_sha256 != content_hash(task.evidence.snapshot)
                    or summary.evidence_catalog != catalog_group(task.evidence)
                ):
                    raise ValueError("摘要不匹配当前业务材料")
            except ValueError:
                raise TaskFactConflict(
                    "context_summary_mismatch", "独立审查需要与当前轮次事实一致的工作摘要"
                ) from None
            summaries.append(record)

        try:
            if len(supplied.messages) != 2 or tuple(m.role for m in supplied.messages) != (
                "system",
                "user",
            ):
                raise ValueError("上下文消息数量错误")
            payload = json.loads(supplied.messages[1].content)
            if not isinstance(payload, dict):
                raise ValueError("上下文必须是对象")
            actual_hypothesis = OpenHypothesisRecord.model_validate(payload.pop("open_hypothesis"))
            if actual_hypothesis != hypothesis:
                raise ValueError("图输入与持久假设不符")
            if "working_summaries" in payload:
                if "reports" in payload or not isinstance(payload["working_summaries"], list):
                    raise ValueError("摘要与独立报告不能同时传入")
                actual_summaries = [
                    WorkingSummaryRecord.model_validate(value)
                    for value in payload.pop("working_summaries")
                ]
                if actual_summaries != summaries:
                    raise ValueError("已组装摘要与持久材料不符")
                payload["reports"] = [
                    summary.summary.report.model_dump(mode="json") for summary in summaries
                ]
            normalized = supplied.model_copy(
                update={
                    "messages": (
                        supplied.messages[0],
                        TextModelMessage(
                            role="user",
                            content=json.dumps(
                                payload,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                    )
                }
            )
        except (KeyError, TypeError, ValueError):
            raise TaskFactConflict(
                "context_input_invalid", "无法解析或核对独立审查上下文"
            ) from None
        validated = validate_context(normalized, sources, operation)
        summary_by_task = {s.task_id: s for s in summaries}
        base_materials = tuple(
            ContextMaterial(
                kind="task_working_summary",
                material_id=material.material_id,
                version=summary_by_task[material.material_id].version,
                content_sha256=summary_by_task[material.material_id].content_sha256,
                evidence_ids=summary_by_task[material.material_id].summary.read_evidence_ids,
            )
            if material.kind == "task_result"
            else material
            for material in validated.materials
        )
        source_manifest = sources.source_synthesis_manifest
        if base_materials != source_manifest.materials:
            raise TaskFactConflict(
                "hypothesis_review_material_mismatch",
                "独立审查材料与开放假设来源综合调用不一致",
            )
        assembled: dict[str, object] = {
            "incident": sources.request.model_dump(mode="json"),
            "evidence": [e.model_dump(mode="json") for e in validated.evidence],
            "working_summaries": [s.model_dump(mode="json") for s in summaries],
            "open_hypothesis": hypothesis.model_dump(mode="json"),
            "task_context": {
                "investigation_run_id": str(sources.run_id),
                "tasks": [
                    {"task_id": str(t.task_id), **t.assignment.model_dump(mode="json")}
                    for t in tasks
                ],
                "completion_requirement": "独立核对支持、反证、缺口和竞争性解释",
            },
        }
        if supplied.structured_output_method == "json_mode":
            assembled["output_json_schema"] = supplied.output_schema
        serialized = json.dumps(
            assembled, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(serialized.encode("utf-8")) > 262144:
            raise CallBudgetExceeded("model_input_too_large")
        prompt_version: ReviewPromptVersion
        role_version: ReviewRoleVersion
        if sources.round_number == 1:
            prompt = HYPOTHESIS_REVIEW_PROMPT
            prompt_version = REVIEW_PROMPT_VERSION
            role_version = "independent-review-v1"
        elif sources.round_number == 2:
            prompt = FOLLOW_UP_HYPOTHESIS_REVIEW_PROMPT
            prompt_version = FOLLOW_UP_REVIEW_PROMPT_VERSION
            role_version = "independent-follow-up-review-v1"
        elif sources.round_number == 3:
            prompt = THIRD_ROUND_HYPOTHESIS_REVIEW_PROMPT
            prompt_version = THIRD_ROUND_REVIEW_PROMPT_VERSION
            role_version = "independent-third-round-review-v1"
        else:
            raise TaskFactConflict("context_operation_invalid", "独立审查轮次不受支持")
        model_input = ModelInput(
            messages=(
                TextModelMessage(role="system", content=prompt),
                TextModelMessage(role="user", content=serialized),
            ),
            output_schema=supplied.output_schema,
            tool_schema=supplied.tool_schema,
            structured_output_method=supplied.structured_output_method,
        )
        materials = base_materials + (
            ContextMaterial(
                kind="open_hypothesis",
                material_id=hypothesis.id,
                version=hypothesis.version,
                content_sha256=hypothesis.content_sha256,
            ),
        )
        allocation = allocate_input(policy, model_input, prompt_version=prompt_version)
        return ContextPacket(
            model_input=model_input,
            manifest=ContextManifest(
                version="summary-context-v1",
                investigation_run_id=sources.run_id,
                task_id=None,
                role="reviewer",
                role_version=role_version,
                workflow_version="v3",
                prompt_version=prompt_version,
                evidence_policy_version=sources.evidence_policy_version,
                materials=materials,
                allocation=allocation,
            ),
        )
