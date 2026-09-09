"""摘要材料使用确定性 JSON 编码，散列不依赖对象字段顺序。"""

import hashlib
import json

from pydantic import BaseModel


def canonical(value: BaseModel) -> bytes:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def material_hash(value: BaseModel) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def serialized_size(value: BaseModel) -> int:
    return len(canonical(value))
