"""确定性操作执行的 PostgreSQL 持久化接口。"""

from opsswarm.execution.persistence.records import (
    ClaimedOperationExecution,
    OperationExecutionOutcome,
    StoredOperationExecution,
)
from opsswarm.execution.persistence.repository import OperationExecutionStore

__all__ = [
    "ClaimedOperationExecution",
    "OperationExecutionOutcome",
    "OperationExecutionStore",
    "StoredOperationExecution",
]
