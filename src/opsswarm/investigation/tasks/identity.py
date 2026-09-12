"""轮次分派使用稳定排序和规范 JSON 身份。"""

import hashlib
import json
from typing import Any

from opsswarm.investigation.tasks.contracts import RoundAssignment


def normalized_round(assignment: RoundAssignment) -> RoundAssignment:
    """任务输出顺序不改变实际轮次身份。"""

    return assignment.model_copy(
        update={"tasks": tuple(sorted(assignment.tasks, key=lambda item: item.task_key))}
    )


def round_specification(assignment: RoundAssignment) -> dict[str, Any]:
    return normalized_round(assignment).model_dump(mode="json", exclude_none=True)


def round_content_hash(assignment: RoundAssignment) -> str:
    specification = round_specification(assignment)
    return hashlib.sha256(
        json.dumps(specification, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
