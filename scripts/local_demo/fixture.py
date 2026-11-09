"""仅为工程链提供固定提案，证据身份始终取自实际输入。"""

import json

from opsswarm.investigation.context.prompts import HYPOTHESIS_REVIEW_PROMPT, OPEN_HYPOTHESIS_PROMPT
from scripts.local_demo.config import FROZEN_HANDOFF_TITLE, MODEL


def _review(ids, *, human=False) -> dict:
    return dict(
        support_is_sufficient=not human,
        rationale="脚本演示独立复查，结论供工程链验证",
        accepted_supporting_evidence_ids=[] if human else ids,
        counter_evidence_ids=[],
        evidence_gaps=["请工程师核对"] if human else [],
        competing_explanations=[],
        suggested_direction="human_review" if human else "evaluate_for_routing",
        follow_up_suggestions=[],
    )


def completion(payload: dict) -> dict:
    if payload.get("model") != MODEL:
        raise ValueError("脚本端点只接受演示模型")
    messages = payload["messages"]
    data = json.loads(messages[1]["content"])
    if not isinstance(data, dict):
        raise ValueError("模型材料必须为对象")
    tools = [tool["function"]["name"] for tool in payload["tools"]]
    name = tools[0]
    incident = data.get("incident")
    frozen_handoff = isinstance(incident, dict) and incident.get("title") == FROZEN_HANDOFF_TITLE
    if "new_evidence" in data:
        items = data["new_evidence"]["evidence"]
        answer = dict(
            specialist="change",
            summary="脚本报告引用新窗口的变更材料",
            findings=["固定报告用于验证工程链，需工程师复核"],
            evidence_ids=[item["evidence_id"] for item in items],
            confidence=0.8,
        )
    elif "next_round_number" in data and "professionals" not in data:
        answer = dict(
            round_number=data["next_round_number"],
            goal="核对失败后的新窗口",
            tasks=[
                dict(
                    task_key="change",
                    role="change",
                    goal="读取新窗口的配置变更",
                    lookback_seconds=600,
                )
            ],
        )
    elif "professionals" in data:
        ids = [
            item["evidence_id"]
            for task in data["professionals"]
            for item in task["evidence"]["evidence"]
        ]
        answer = (
            _review(ids)
            if "synthesis_result" in data
            else dict(
                statement="业务未恢复，引用新窗口材料供独立复查",
                confidence=0.8,
                supporting_evidence_ids=ids,
                counter_evidence_ids=[],
                unresolved_questions=["需要工程师决定后续处置"],
            )
        )
    elif "capability_catalog" in data:
        answer = dict(
            status="candidate",
            action_type="rollback_release",
            rationale="脚本演示回滚候选，执行资格仍由核验和审批决定",
            evidence_ids=data["current_review"]["accepted_supporting_evidence_ids"][:1],
            preconditions=["确认当前演练版本与发布依据一致"],
            unresolved_questions=[],
        )
    elif "original_review" in data and "human_inputs" in data:
        answer = _review(
            data["original_investigation"]["open_hypothesis"]["hypothesis"][
                "supporting_evidence_ids"
            ][:1]
        )
    elif "policy" in data and "round_number" in data:
        roles = ("log", "metric") if frozen_handoff else ("log", "metric", "change")
        answer = dict(
            round_number=1,
            goal="按专业读取现场材料",
            tasks=[
                dict(task_key=role, role=role, goal=f"核对 {role} 专业材料", lookback_seconds=600)
                for role in roles
            ],
        )
    elif "open_hypothesis" in data:
        answer = _review([], human=True)
        if frozen_handoff and messages[0]["content"] == HYPOTHESIS_REVIEW_PROMPT:
            answer.update(
                rationale="日志与指标说明故障存在，仍需核对发布记录",
                evidence_gaps=["缺少结构化发布变更"],
                suggested_direction="follow_up",
                follow_up_suggestions=[
                    dict(role="change", question="故障前有哪些发布变更？", rationale="核对发布依据")
                ],
            )
    elif "working_summaries" not in data:
        catalog = data["evidence_catalog"]
        if len(messages) == 2 and catalog["entries"]:
            name = "read_evidence"
            answer = dict(
                evidence_id=catalog["entries"][0]["evidence_id"],
                snapshot_sha256=catalog["snapshot_sha256"],
            )
        else:
            name = "SpecialistReport"
            detail = json.loads(messages[-1]["content"]) if len(messages) > 2 else {}
            item = detail.get("evidence")
            answer = dict(
                specialist=data["specialist"],
                summary="脚本专业报告引用授权材料",
                findings=["固定输出用于验证取证、预算和持久化"],
                evidence_ids=[item["evidence_id"]] if item else [],
                confidence=0.7,
            )
    else:
        first_frozen = frozen_handoff and messages[0]["content"] == OPEN_HYPOTHESIS_PROMPT
        evidence = data["evidence"]
        support = (
            evidence if first_frozen else [item for item in evidence if item["kind"] == "change"]
        )
        if not support:
            raise ValueError("缺少实际材料，不能制造演示依据")
        answer = dict(
            statement=(
                "固定日志与指标记录了业务失败，仍需补充发布依据"
                if first_frozen
                else "发布记录与业务失败的关系需要工程师核对"
            ),
            confidence=0.7,
            supporting_evidence_ids=[support[0]["evidence_id"]],
            counter_evidence_ids=[],
            unresolved_questions=["等待人工核对当前演练发布"],
        )
    if name not in tools:
        raise ValueError("响应工具不在本次授权集合内")
    return dict(
        id="local-demo-completion",
        object="chat.completion",
        created=0,
        model=MODEL,
        choices=[
            dict(
                index=0,
                finish_reason="tool_calls",
                message=dict(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        dict(
                            id=f"demo-{name}",
                            type="function",
                            function=dict(
                                name=name, arguments=json.dumps(answer, ensure_ascii=False)
                            ),
                        )
                    ],
                ),
            )
        ],
        # 固定用量只属于脚本模型，不是实际模型计费或推理统计。
        usage=dict(prompt_tokens=80, completion_tokens=20, total_tokens=100),
    )
