"""构造兼容历史调用、由运行层固定的预留身份。"""

from typing import Annotated
from uuid import UUID

from pydantic import Field, TypeAdapter

from opsswarm.investigation.budget.contracts import CallOperation

InvocationIndex = Annotated[int, Field(ge=0, le=99, strict=True)]
_index_adapter = TypeAdapter(InvocationIndex)


def reservation_key(
    parent_id: UUID,
    task_attempt_id: UUID | None,
    operation: CallOperation,
    invocation_index: int,
) -> str:
    index = _index_adapter.validate_python(invocation_index)
    base = f"{parent_id}:{task_attempt_id or 'root'}:{operation.value}"
    # 旧 checkpoint 重放仍必须命中原预留，不能因升级再次发生外部调用。
    return base if index == 0 else f"{base}:{index}"
