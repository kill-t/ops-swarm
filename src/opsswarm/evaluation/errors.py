"""评测报告持久化领域错误。"""

from uuid import UUID


class EvaluationError(Exception):
    """评测持久化与读取错误基类。"""


class EvaluationRunNotFound(EvaluationError):
    """请求的评测运行不存在。"""

    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__(f"评测运行不存在：{run_id}")


class EvaluationAttemptNotFound(EvaluationError):
    """请求的真实模型评测尝试不存在。"""

    def __init__(self, attempt_id: UUID) -> None:
        self.attempt_id = attempt_id
        super().__init__(f"评测尝试不存在：{attempt_id}")


class EvaluationAttemptConflict(EvaluationError):
    """幂等键或实验槽位已经绑定另一条尝试。"""

    def __init__(self, identity: str) -> None:
        self.identity = identity
        super().__init__(f"评测尝试标识冲突：{identity}")


class EvaluationAttemptAlreadyFinished(EvaluationError):
    """终态尝试不能被第二次改写。"""

    def __init__(self, attempt_id: UUID) -> None:
        self.attempt_id = attempt_id
        super().__init__(f"评测尝试已经结束：{attempt_id}")


class EvaluationBudgetExceeded(EvaluationError):
    """真实模型尝试触及调用、Token 或费用护栏。"""


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


class EvaluationExperimentConflict(EvaluationError):
    """同一实验标识对应了不同的可比配置。"""

    def __init__(self, experiment_key: str) -> None:
        self.experiment_key = experiment_key
        super().__init__(f"评测实验配置冲突：{experiment_key}")


class EvaluationRepetitionConflict(EvaluationError):
    """同一实验的重复序号已经被其他运行占用。"""

    def __init__(self, experiment_key: str, repetition_index: int) -> None:
        self.experiment_key = experiment_key
        self.repetition_index = repetition_index
        super().__init__(f"评测重复序号已被占用：{experiment_key}#{repetition_index}")


class EvaluationReportMismatch(EvaluationError):
    """报告与其声明的数据集或持久快照不一致。"""
