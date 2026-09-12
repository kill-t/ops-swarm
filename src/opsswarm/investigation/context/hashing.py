"""上下文材料的确定性内容身份。"""

import hashlib
import json

from pydantic import BaseModel


def content_hash(value: BaseModel) -> str:
    """材料引用使用完整内容散列，JSON 对象顺序不影响身份。"""

    return hashlib.sha256(
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
