"""为现场回流构造有界模型视图，完整冻结来源仍用于核验。"""

import json
from typing import Any

from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError

PROJECTION_POLICY = "reflow-projection-v1"


def project_source(source: dict[str, Any]) -> dict[str, Any]:
    original = next(
        m["content"] for m in source["materials"] if m["kind"] == "original_investigation"
    )
    if "input_policy" not in original:
        return source

    try:
        if original["input_policy"] != PROJECTION_POLICY:
            raise ValueError
        candidate = original["candidate_source"]
        (user_message,) = [
            message["content"]
            for message in candidate["prior_input"]["messages"]
            if message["role"] == "user"
        ]
        prior = json.loads(user_message)
        if not isinstance(prior, dict) or set(prior) - {"output_json_schema"} != {
            "incident",
            "evidence",
            "open_hypothesis",
            "task_context",
            "working_summaries",
        }:
            raise ValueError
        history = {
            "incident": prior["incident"],
            "task_context": prior["task_context"],
            "working_summaries": prior["working_summaries"],
            "evidence": candidate["evidence"],
            "hypothesis": candidate["hypothesis"],
            "failed_calls": original["failed_calls"],
            "input_policy": PROJECTION_POLICY,
        }
        for old, current in (("evidence", "evidence"), ("open_hypothesis", "hypothesis")):
            if prior[old] != history[current]:
                history[f"prior_{old}"] = prior[old]
        if candidate["human_route"] is not None:
            history["unresolved_constraints"] = candidate["human_route"]["decision"][
                "unresolved_constraints"
            ]
    except (KeyError, TypeError, ValueError) as error:
        raise ReflowError(
            "reflow_projection_invalid", "回流历史业务输入无法按固定策略识别"
        ) from error

    materials = []
    for material in source["materials"]:
        kind = material["kind"]
        content = material["content"]
        if kind == "original_investigation":
            content = history
        elif kind == "execution":
            content = {"result": content["result"]}
        elif kind == "admission":
            materials.append({key: value for key, value in material.items() if key != "content"})
            continue
        materials.append(material | {"content": content})

    projected = {
        key: source[key]
        for key in (
            "version",
            "investigation_run_id",
            "next_round_number",
            "existing_task_count",
            "maximum_tasks",
            "coordination_policy",
        )
    }
    projected["materials"] = materials
    if "professionals" in source:
        projected["professionals"] = [
            {
                "task_id": professional["task_id"],
                "assignment": professional["assignment"],
                "effective_window": professional["effective_window"],
                "evidence": professional["evidence"],
                "report": professional["result"]["report"],
            }
            for professional in source["professionals"]
        ]
    if "synthesis_result" in source:
        projected["synthesis_result"] = source["synthesis_result"]
    return projected
