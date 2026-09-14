"""从已校验的业务材料确定性组装输入，不执行 SQL、HTTP 或模型调用。"""

import json

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_contracts import (
    ModelBudgetPolicy,
    ModelInput,
    TextModelMessage,
)
from opsswarm.investigation.budget.model_policy import allocate_input
from opsswarm.investigation.context.contracts import (
    CONTEXT_PROMPT_VERSION,
    ContextManifest,
    ContextPacket,
    ContextSources,
)
from opsswarm.investigation.context.hashing import content_hash as content_hash
from opsswarm.investigation.context.prompts import SPECIALIST_PROMPT, SYNTHESIS_PROMPT
from opsswarm.investigation.context.validation import validate_context


class ContextBuilder:
    def build(
        self,
        supplied: ModelInput,
        sources: ContextSources,
        operation: CallOperation,
        policy: ModelBudgetPolicy,
    ) -> ContextPacket:
        validated = validate_context(supplied, sources, operation)
        role, tasks = validated.role, validated.tasks
        evidence, reports, materials = validated.evidence, validated.reports, validated.materials
        assembled: dict[str, object] = {
            "incident": sources.request.model_dump(mode="json"),
            "evidence": [e.model_dump(mode="json") for e in evidence],
            "task_context": {
                "investigation_run_id": str(sources.run_id),
                "tasks": [
                    {"task_id": str(t.task_id), **t.assignment.model_dump(mode="json")}
                    for t in tasks
                ],
                "completion_requirement": "输出引用输入证据的结构化专业报告"
                if role
                else "综合已提交报告，证据不足时保留未确定判断",
            },
        }
        if role:
            assembled["specialist"] = role.value
        else:
            assembled["reports"] = [r.model_dump(mode="json") for r in reports]
        prompt = SPECIALIST_PROMPT if role else SYNTHESIS_PROMPT
        if supplied.structured_output_method == "json_mode":
            prompt += " 必须只输出符合下方 JSON Schema 的 JSON 对象。"
            assembled["output_json_schema"] = supplied.output_schema
        model_input = ModelInput(
            messages=(
                TextModelMessage(role="system", content=prompt),
                TextModelMessage(
                    role="user",
                    content=json.dumps(
                        assembled, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ),
                ),
            ),
            output_schema=supplied.output_schema,
            tool_schema=supplied.tool_schema,
            structured_output_method=supplied.structured_output_method,
        )
        allocation = allocate_input(policy, model_input, prompt_version=CONTEXT_PROMPT_VERSION)
        return ContextPacket(
            model_input=model_input,
            manifest=ContextManifest(
                version=sources.context_policy_version,
                investigation_run_id=sources.run_id,
                task_id=sources.task_id,
                role=role.value if role else "synthesis",
                workflow_version=sources.workflow_version,
                evidence_policy_version=sources.evidence_policy_version,
                materials=tuple(materials),
                allocation=allocation,
            ),
        )
