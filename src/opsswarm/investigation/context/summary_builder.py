"""将已完成工作的不可变摘要送入综合节点，保留全部有界证据。"""

import json

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
from opsswarm.investigation.context.prompts import SUMMARY_PROMPT
from opsswarm.investigation.context.validation import validate_context
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.working_summary.contracts import WorkingSummaryRecord

SUMMARY_PROMPT_VERSION = "summary-context-v1"


class SummaryContextBuilder:
    def build(
        self,
        supplied: ModelInput,
        sources: ContextSources,
        operation: CallOperation,
        policy: ModelBudgetPolicy,
    ) -> ContextPacket:
        if sources.context_policy_version != SUMMARY_PROMPT_VERSION or operation != (
            CallOperation.SYNTHESIZE
        ):
            raise TaskFactConflict("context_operation_invalid", "工作摘要仅供新版综合节点消费")
        tasks = sorted(sources.tasks, key=lambda task: task.assignment.role.value)
        summaries: list[WorkingSummaryRecord] = []
        for task in tasks:
            try:
                if task.working_summary is None:
                    raise ValueError("缺少工作摘要")
                record = WorkingSummaryRecord.model_validate(
                    task.working_summary.model_dump(mode="json")
                )
                summary = record.summary
                if (
                    task.result is None
                    or record.task_id != task.task_id
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
                    "context_summary_mismatch", "综合需要与任务、结果和证据一致的工作摘要"
                ) from None
            # 原始调用及工具配对由加载器和预留事务重算；本层不从摘要推断调用真实性。
            summaries.append(record)

        normalized = supplied
        try:
            if len(supplied.messages) != 2 or tuple(m.role for m in supplied.messages) != (
                "system",
                "user",
            ):
                raise ValueError("上下文消息数量错误")
            payload = json.loads(supplied.messages[1].content)
            if not isinstance(payload, dict):
                raise ValueError("上下文必须是对象")
            if "working_summaries" in payload:
                if "reports" in payload or not isinstance(payload["working_summaries"], list):
                    raise ValueError("摘要与独立报告不能同时传入")
                actual = [
                    WorkingSummaryRecord.model_validate(value)
                    for value in payload.pop("working_summaries")
                ]
                if actual != summaries:
                    raise ValueError("已组装摘要与持久材料不符")
                payload["reports"] = [s.summary.report.model_dump(mode="json") for s in summaries]
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
        except (ValueError, TypeError):
            raise TaskFactConflict(
                "context_input_invalid", "无法解析或核对工作摘要上下文"
            ) from None
        validated = validate_context(normalized, sources, operation)
        assembled: dict[str, object] = {
            "incident": sources.request.model_dump(mode="json"),
            "evidence": [e.model_dump(mode="json") for e in validated.evidence],
            "working_summaries": [s.model_dump(mode="json") for s in summaries],
            "task_context": {
                "investigation_run_id": str(sources.run_id),
                "tasks": [
                    {"task_id": str(t.task_id), **t.assignment.model_dump(mode="json")}
                    for t in tasks
                ],
                "completion_requirement": "综合工作摘要与完整证据，保留分歧和未确定判断",
            },
        }
        prompt = SUMMARY_PROMPT
        if supplied.structured_output_method == "json_mode":
            prompt += " 必须只输出符合下方 JSON Schema 的 JSON 对象。"
            assembled["output_json_schema"] = supplied.output_schema
        serialized = json.dumps(
            assembled, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(serialized.encode("utf-8")) > 262144:
            raise CallBudgetExceeded("model_input_too_large")
        model_input = ModelInput(
            messages=(
                TextModelMessage(role="system", content=prompt),
                TextModelMessage(
                    role="user",
                    content=serialized,
                ),
            ),
            output_schema=supplied.output_schema,
            tool_schema=supplied.tool_schema,
            structured_output_method=supplied.structured_output_method,
        )
        summary_by_task = {s.task_id: s for s in summaries}
        materials = tuple(
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
        # 仅对实际发送的完整输入做一次预算检查；必要材料超限时整体拒绝，不裁剪。
        allocation = allocate_input(policy, model_input, prompt_version=SUMMARY_PROMPT_VERSION)
        return ContextPacket(
            model_input=model_input,
            manifest=ContextManifest(
                version="summary-context-v1",
                investigation_run_id=sources.run_id,
                task_id=None,
                role="synthesis",
                prompt_version="summary-context-v1",
                evidence_policy_version=sources.evidence_policy_version,
                materials=materials,
                allocation=allocation,
            ),
        )
