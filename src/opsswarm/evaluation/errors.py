"""评测报告持久化领域错误。"""

from uuid import UUID


class EvaluationError(Exception):
    """评测持久化与读取错误基类。"""


class EvaluationRunNotFound(EvaluationError):
    """请求的评测运行不存在。"""

    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__(f"评测运行不存在：{run_id}")


class EvaluationIdempotencyConflict(EvaluationError):
    """同一幂等键被用于不同评测报告。"""

    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(f"评测幂等键已绑定其他报告：{idempotency_key}")


class EvaluationDatasetVersionConflict(EvaluationError):
    """同一数据集版本标识对应了不同内容。"""

    def __init__(self, dataset_key: str) -> None:
        self.dataset_key = dataset_key
        super().__init__(f"评测集版本内容冲突：{dataset_key}")


class EvaluationReportMismatch(EvaluationError):
    """报告与其声明的数据集或持久快照不一致。"""
