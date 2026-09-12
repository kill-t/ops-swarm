"""协调策略与计划使用确定性 JSON 内容身份。"""

import hashlib
import json

from pydantic import BaseModel


def coordination_hash(value: BaseModel) -> str:
    return hashlib.sha256(
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
